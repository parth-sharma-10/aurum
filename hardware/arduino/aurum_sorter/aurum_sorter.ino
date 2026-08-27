/*
 * Aurum - sorter firmware. Weight sensing + Servo A/B actuation.
 *
 * RUN ON HARDWARE. The Python side has opened a real serial port to an
 * attached Arduino, sent MOVE, and had it acknowledged. What no one has done
 * is watch a paddle: see docs/hardware.md, and scripts/bench_check.py.
 *
 * The boot banner says which build is on the board:
 *
 *   AURUM/1 BOOT <FIRMWARE_BUILD> rest=<restAngle>
 *
 * An older build bannered `SERVO_INIT B rest=90` - a string that appears
 * nowhere in this repository. If you see that, the board is NOT running this
 * file and its rest angle is 90 where this file says 0. Reflash.
 *
 * For CALIBRATION use hardware/arduino/aurum_weight/ instead: it has no servo
 * code at all, so nothing can move while you are handling reference masses.
 *
 * Wiring, as physically built and bench-tested:
 *
 *   HX711 DOUT     -> D2
 *   HX711 SCK      -> D3
 *   Servo A signal -> D9
 *   Servo B signal -> D10
 *   L298N ENA      -> D5    (PWM, conveyor speed)
 *   L298N IN1      -> D7
 *   L298N IN2      -> D8
 *
 * Servo power comes from an external AKSHA 5V/3A supply whose ground is common
 * with the Arduino. The external +5V rail is NOT connected to the Arduino +5V
 * pin and must never be. An earlier setup shorted and melted jumper wires.
 *
 * Serial: 115200 baud. Must match conveyor.arduino.baudrate.
 *
 * Frames out (weight, continuous, unchanged from Phase 5):
 *   W,1,<millis>,<raw_counts>,OK|ERR
 *
 * Frames in / out (commands):
 *   in   AURUM/1 MOVE <A|B> <item_id> <command_id>
 *   in   AURUM/1 CFG <rest_deg> <push_deg> <hold_ms> <command_id>
 *   in   AURUM/1 PING <command_id>
 *   in   AURUM/1 BELT RUN <pwm 0-255> <command_id>
 *   in   AURUM/1 BELT STOP <command_id>
 *   out  AURUM/1 BELTSTOP WATCHDOG        (the lease expired; motor stopped)
 *   out  AURUM/1 ACK <command_id> [DUP]
 *   out  AURUM/1 ERR <command_id> <code>
 *   out  AURUM/1 PONG <command_id>
 *
 * RAW COUNTS, never grams: calibration lives in Python, in version control,
 * next to the workflow that produced it. See docs/hardware.md.
 *
 * Servo angles are NOT compiled in as final geometry. They arrive over CFG so
 * they can be tuned from configs/conveyor.yaml without reflashing. The
 * defaults below are BENCH/TEST values and have never deflected an item -
 * there is no conveyor for an item to travel on.
 */

#include <Servo.h>

const uint8_t PIN_DOUT   = 2;
const uint8_t PIN_SCK    = 3;
const uint8_t PIN_SERVO_A = 9;
const uint8_t PIN_SERVO_B = 10;
// Status LED. Lit while a paddle is mid-stroke, so the one moment the machine
// is physically committed is visible from across a room without reading a
// serial log. Also blinks a signature at boot - see setup().
const uint8_t PIN_LED    = 6;

// L298N conveyor motor driver, as physically wired:
//   ENA -> D5   PWM speed. Timer0/OC0B - HARDWARE pwm, so unlike the Servo
//               library's ISR-driven pulses it CANNOT be corrupted by the
//               301 us `noInterrupts()` in readRaw(). analogWrite here does
//               not disturb millis(), which uses Timer0's overflow, not OC0B.
//   IN1 -> D7   direction
//   IN2 -> D8   direction
//   OUT1/OUT2 -> motor, VCC -> 12 V, GND common with the Arduino.
const uint8_t PIN_BELT_ENA = 5;
const uint8_t PIN_BELT_IN1 = 7;
const uint8_t PIN_BELT_IN2 = 8;

// THE BELT STOPS ON ITS OWN IF THE HOST STOPS ASKING.
//
// Every other failure in this machine is static - a paddle that does not move,
// a mass that will not settle. A belt is the one part that keeps acting after
// the software controlling it has gone, so `BELT RUN` is a lease rather than a
// switch: the host must re-assert it, and if it does not - crashed backend,
// unplugged USB, killed process - the motor stops without anybody present.
//
// Generously longer than the ~1.7 s a `push()` blocks for, so a paddle stroke
// never starves the lease and stalls the belt mid-run.
const unsigned long BELT_WATCHDOG_MS = 3000;

const uint8_t  PROTOCOL_VERSION   = 1;
const unsigned long SAMPLE_INTERVAL_MS = 100;   // 10 Hz, the HX711 rate
const unsigned long READY_TIMEOUT_MS   = 500;

// Printed at boot and blinked on the LED. Bump it whenever this file changes,
// so "which build is on the board" is answerable from the wire and from across
// the room instead of being inferred.
#define FIRMWARE_BUILD "2026-08-27e"

// How long to give a paddle to physically reach an angle before the pins are
// released. An MG995 is about 0.2 s per 60 degrees, so a full rest<->push
// throw is ~0.3 s; 500 ms covers it with margin. Too short and `detach()`
// cuts the pulse train mid-travel, leaving the paddle wherever it got to.
const unsigned long SERVO_TRAVEL_MS = 500;

// MEASURED, frame to ACK, on this board: PING 0.007 s, CFG 0.009 s, MOVE
// 1.711 s = 500 (settle at rest) + 700 (holdMs) + 500 (return). The paddle
// therefore starts moving ~500 ms after the MOVE frame, and that figure is
// what `conveyor.timing.servo_actuation_delay_ms` must carry - the scheduler
// fires this much EARLY so the stroke lands on the item.
//
// The 500 ms settle is longer than it needs to be: the paddle is already
// parked at rest, so only a couple of pulse periods are required to establish
// the train. Shortening it to ~40 ms would cut the actuation delay by an order
// of magnitude. NOT DONE HERE, because it must be flashed and re-measured
// together, and the board must never run a build this file does not describe.

// Heartbeat while idle. A board that has stopped executing keeps its LED
// frozen instead of blinking, which is the cheapest possible detector for the
// hang seen on the bench (millis() stuck, the same counts repeating for ever).
const unsigned long HEARTBEAT_MS = 1000;

// BENCH/TEST defaults, overridden by the CFG frame at startup.
int restAngle  = 0;
int pushAngle  = 90;
unsigned long holdMs = 700;

Servo servoA;
Servo servoB;

// Recent command ids, so an ACK lost on the wire cannot cause a second
// movement when the host resends. The host also suppresses duplicates; this is
// the last barrier, and the one that matters because it is closest to the
// paddle.
const uint8_t HISTORY = 8;
String recentIds[HISTORY];
uint8_t historyNext = 0;

String inputLine;

// Belt state. `beltPwm` is kept so the snapshot and the ACK can report what is
// actually being driven rather than what was last asked for.
bool beltRunning = false;
uint8_t beltPwm = 0;
unsigned long beltLeaseAt = 0;

void beltStop() {
  // ENA to 0 first, then both direction lines low: the motor coasts rather
  // than being actively braked, which is what an L298N does with IN1 == IN2.
  // Order matters - dropping direction while still enabled briefly brakes.
  analogWrite(PIN_BELT_ENA, 0);
  digitalWrite(PIN_BELT_IN1, LOW);
  digitalWrite(PIN_BELT_IN2, LOW);
  beltRunning = false;
  beltPwm = 0;
}

void beltRun(uint8_t pwm) {
  digitalWrite(PIN_BELT_IN1, HIGH);
  digitalWrite(PIN_BELT_IN2, LOW);
  analogWrite(PIN_BELT_ENA, pwm);
  beltRunning = true;
  beltPwm = pwm;
  beltLeaseAt = millis();
}

bool alreadyDone(const String &id) {
  for (uint8_t i = 0; i < HISTORY; i++) {
    if (recentIds[i] == id) return true;
  }
  return false;
}

void remember(const String &id) {
  recentIds[historyNext] = id;
  historyNext = (historyNext + 1) % HISTORY;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_DOUT, INPUT);
  pinMode(PIN_SCK, OUTPUT);
  digitalWrite(PIN_SCK, LOW);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  // THE BELT IS STOPPED BEFORE ANYTHING ELSE. A reset must never leave a
  // motor running, and the pins float until they are driven, so claim them
  // first and drive them low.
  pinMode(PIN_BELT_ENA, OUTPUT);
  pinMode(PIN_BELT_IN1, OUTPUT);
  pinMode(PIN_BELT_IN2, OUTPUT);
  beltStop();

  // Park at rest before anything else, so a reset cannot leave a paddle out in
  // the stream - then RELEASE both pins. See `park()` and `push()` below for
  // why nothing stays attached.
  park(servoA, PIN_SERVO_A);
  park(servoB, PIN_SERVO_B);
  inputLine.reserve(64);

  // Three quick blinks, then the banner. Both exist for the same reason: the
  // board on the bench spent weeks running a build that was not this file and
  // nothing said so. A boot signature you can SEE, plus one you can grep,
  // means "did the reflash take?" stops being a guess.
  for (uint8_t i = 0; i < 3; i++) {
    digitalWrite(PIN_LED, HIGH);
    delay(80);
    digitalWrite(PIN_LED, LOW);
    delay(80);
  }
  Serial.print("AURUM/1 BOOT ");
  Serial.print(FIRMWARE_BUILD);
  Serial.print(" rest=");
  Serial.println(restAngle);
}

// ---------------------------------------------------------------- HX711

bool waitReady(unsigned long timeoutMs) {
  unsigned long start = millis();
  while (digitalRead(PIN_DOUT) == HIGH) {
    if (millis() - start > timeoutMs) return false;
  }
  return true;
}

// The datasheet's own power-down: SCK held HIGH for >60 us powers the HX711
// off, and dropping it LOW brings it back reset, on channel A at gain 128 and
// re-running its internal calibration.
//
// Used as recovery, because that is also the ACCIDENTAL way to power it down.
// A DOUT line stuck HIGH for ever - `waitReady` timing out at READY_TIMEOUT_MS
// over and over, which is the 500 ms `0,ERR` cadence seen on this bench - is
// what a powered-down converter looks like from here. Without this the board
// emits ERR until somebody unplugs it; with it, the cell comes back on its own.
void resetHx711() {
  digitalWrite(PIN_SCK, HIGH);
  delayMicroseconds(80);          // >60 us: the power-down threshold
  digitalWrite(PIN_SCK, LOW);
  delayMicroseconds(80);
}

long readRaw() {
  long value = 0;
  // INTERRUPTS OFF FOR THE WHOLE PULSE TRAIN. Keep it that way.
  //
  // An ISR landing between `digitalWrite(PIN_SCK, HIGH)` and the matching LOW
  // stretches that clock pulse, and a pulse longer than 60 us IS the HX711's
  // power-down command: the converter goes away mid-conversion and DOUT never
  // falls again. That is the intermittent lockup - the cell reading cleanly on
  // one boot and returning `0,ERR` for ever on the next with nothing about the
  // wiring different.
  //
  // THE COST IS NOT ~50 us. Measured against real AVR costs, stock
  // digitalWrite/digitalRead are ~3.4 us each on a 16 MHz Uno, so one bit is
  // ~12 us and the whole train is ~301 us. An earlier comment here claimed
  // ~50 us and that error invited an attempt to "fix" it by re-enabling
  // interrupts between bits. DO NOT DO THAT. Measured on this bench, same pan,
  // same 60 s procedure:
  //
  //     interrupts off for the whole train : stdev    33 counts (0.08 g)
  //     interrupts re-enabled between bits : stdev 10232 counts (26.09 g)
  //
  // 310x worse. Servo pulses firing during a microvolt-level bridge read
  // couple straight into it, so this block is not only about SCK timing - it
  // is what keeps the read electrically quiet.
  //
  // What 301 us DOES break is the servo, if one is attached: it delays the
  // Servo library's Timer1 ISR and stretches whatever pulse it lands in by up
  // to 301 us, which at ~10.3 us per degree is up to 29 degrees of unwanted
  // travel. That is fixed where it belongs - `push()` and `park()` attach a
  // servo only while commanding it, so while this runs there is no servo ISR
  // to delay and no pulse to corrupt.
  noInterrupts();
  for (uint8_t i = 0; i < 24; i++) {
    digitalWrite(PIN_SCK, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | digitalRead(PIN_DOUT);
    digitalWrite(PIN_SCK, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(PIN_SCK, HIGH);   // 25th pulse: channel A, gain 128
  delayMicroseconds(1);
  digitalWrite(PIN_SCK, LOW);
  interrupts();
  delayMicroseconds(1);
  if (value & 0x800000L) value |= ~0xFFFFFFL;
  return value;
}

void emitWeight(long counts, const char *status) {
  Serial.print("W,");
  Serial.print(PROTOCOL_VERSION);
  Serial.print(',');
  Serial.print(millis());
  Serial.print(',');
  Serial.print(counts);
  Serial.print(',');
  Serial.println(status);
}

// ------------------------------------------------------------- commands

void ack(const String &id, const char *suffix) {
  Serial.print("AURUM/1 ACK ");
  Serial.print(id);
  if (suffix[0] != '\0') { Serial.print(' '); Serial.print(suffix); }
  Serial.println();
}

void err(const String &id, const char *code) {
  Serial.print("AURUM/1 ERR ");
  Serial.print(id);
  Serial.print(' ');
  Serial.println(code);
}

// A SERVO IS ATTACHED ONLY WHILE IT IS BEING COMMANDED. Everywhere else both
// pins are released, and this is the single most important property in this
// file. Two independent faults come from leaving them attached:
//
//   THE PADDLES TWITCH ON THEIR OWN. The Servo library builds its pulse width
//   in a Timer1 ISR. `readRaw()` blocks interrupts for the whole 25-pulse
//   train - ~301 us measured against real AVR costs, not the ~50 us its
//   comment used to claim - so whichever servo pulse that lands in is
//   stretched by up to 301 us. An AVR servo is ~10.3 us per degree, so that is
//   up to 29 DEGREES of unwanted travel, roughly 18 times a minute at 10 Hz
//   sampling. No MOVE, no route, no command: just a corrupted pulse.
//
//   THE LOAD CELL GETS NOISY. Servo pulses firing during a microvolt-level
//   bridge read couple straight into it. MEASURED on this bench, same pan,
//   same 60 s procedure: attached and interrupt-interleaved gave a standard
//   deviation of 10232 counts (26.09 g); with the read left electrically quiet
//   it is 33 counts (0.08 g). A factor of 310.
//
// Detaching fixes both at once rather than trading one for the other, because
// `Servo::detach()` calls `finISR()` once the last servo goes inactive: while
// the machine is idle there is no servo ISR at all, so there is no pulse to
// corrupt and nothing to couple into the cell. `readRaw()` keeps its
// whole-train `noInterrupts()`, which now costs nothing.
//
// The trade is that a released paddle has no holding torque. These are
// horizontal and stay put; a spring- or gravity-loaded paddle would need a
// mechanical detent rather than a permanently energised servo.
void park(Servo &servo, uint8_t pin) {
  servo.attach(pin);
  // Immediately, before the ISR can emit a pulse: `attach()` seeds the channel
  // with DEFAULT_PULSE_WIDTH (1500 us = 90 deg), and on this geometry 90 deg
  // is the PUSH angle. Left for even one pulse that would fire the paddle.
  servo.write(restAngle);
  delay(SERVO_TRAVEL_MS);
  servo.detach();
}

// Blocking on purpose. One paddle moves at a time, weight sampling pauses for
// the stroke, and there is nothing else this board should be doing meanwhile.
void push(Servo &servo, uint8_t pin) {
  digitalWrite(PIN_LED, HIGH);    // lit for exactly as long as the paddle is out
  servo.attach(pin);
  servo.write(restAngle);         // claim the current position before any pulse
  delay(SERVO_TRAVEL_MS);         // hold at rest before committing to the throw
  servo.write(pushAngle);
  delay(holdMs);
  servo.write(restAngle);
  delay(SERVO_TRAVEL_MS);         // reach rest BEFORE the pulses stop
  servo.detach();
  digitalWrite(PIN_LED, LOW);
}

String field(const String &line, uint8_t index) {
  uint8_t found = 0;
  int start = 0;
  for (int i = 0; i <= line.length(); i++) {
    if (i == line.length() || line.charAt(i) == ' ') {
      if (found == index) return line.substring(start, i);
      found++;
      start = i + 1;
    }
  }
  return "";
}

void handleCommand(const String &line) {
  if (!line.startsWith("AURUM/1 ")) return;   // not for us; ignore silently

  String verb = field(line, 1);

  if (verb == "PING") {
    Serial.print("AURUM/1 PONG ");
    Serial.println(field(line, 2));
    return;
  }

  if (verb == "CFG") {
    String id = field(line, 5);
    if (id.length() == 0) { err(field(line, 2), "BAD_FRAME"); return; }
    restAngle = field(line, 2).toInt();
    pushAngle = field(line, 3).toInt();
    holdMs    = (unsigned long) field(line, 4).toInt();
    // ANGLES ONLY. CFG used to `write()` both servos here, which made
    // configuration a physical movement - and the host sends CFG on every
    // connect and every automatic reconnect, so opening the dashboard moved
    // both paddles. A configuration frame must never actuate. The new angles
    // take effect at the next `park()` or `push()`, which are the only two
    // places a pin is ever driven.
    ack(id, "");
    return;
  }

  if (verb == "BELT") {
    String action = field(line, 2);
    if (action == "RUN") {
      String id = field(line, 4);
      if (id.length() == 0) { err(field(line, 3), "BAD_FRAME"); return; }
      long pwm = field(line, 3).toInt();
      if (pwm < 0 || pwm > 255) { err(id, "BAD_PWM"); return; }
      // NOT duplicate-suppressed, unlike MOVE. A repeated RUN is how the host
      // renews the watchdog lease, and it is idempotent: it asserts a state
      // rather than performing an action. Suppressing it would stop the belt.
      beltRun((uint8_t) pwm);
      ack(id, "");
      return;
    }
    if (action == "STOP") {
      String id = field(line, 3);
      if (id.length() == 0) { err(action, "BAD_FRAME"); return; }
      beltStop();
      ack(id, "");
      return;
    }
    err(field(line, 3), "BAD_FRAME");
    return;
  }

  if (verb == "MOVE") {
    String target = field(line, 2);
    String id     = field(line, 4);
    if (id.length() == 0) { err(id, "BAD_FRAME"); return; }

    if (alreadyDone(id)) { ack(id, "DUP"); return; }   // never move twice

    if (target == "A")      { remember(id); push(servoA, PIN_SERVO_A); ack(id, ""); }
    else if (target == "B") { remember(id); push(servoB, PIN_SERVO_B); ack(id, ""); }
    else                    { err(id, "BAD_TARGET"); }   // C has no servo
    return;
  }

  err(field(line, 2), "BAD_FRAME");
}

// ------------------------------------------------------------------ loop

unsigned long lastSample = 0;
unsigned long lastBeat = 0;
bool beatOn = false;

void loop() {
  while (Serial.available()) {
    char c = (char) Serial.read();
    if (c == '\n') {
      inputLine.trim();
      if (inputLine.length()) handleCommand(inputLine);
      inputLine = "";
    } else if (c != '\r' && inputLine.length() < 60) {
      inputLine += c;
    }
  }

  // The belt's lease. Checked before anything else in the loop, so a host that
  // has gone away stops the motor even if the rest of this loop is starved.
  if (beltRunning && millis() - beltLeaseAt > BELT_WATCHDOG_MS) {
    beltStop();
    Serial.println("AURUM/1 BELTSTOP WATCHDOG");
  }

  // Idle heartbeat. `push()` drives the LED solid for the whole stroke and
  // this only runs between strokes, so the two never fight over the pin.
  if (millis() - lastBeat >= HEARTBEAT_MS) {
    lastBeat = millis();
    beatOn = !beatOn;
    digitalWrite(PIN_LED, beatOn ? HIGH : LOW);
  }

  if (millis() - lastSample >= SAMPLE_INTERVAL_MS) {
    lastSample = millis();
    if (waitReady(READY_TIMEOUT_MS)) {
      emitWeight(readRaw(), "OK");
    } else {
      // Say so rather than repeating the last good value: Python drops any
      // line that is not OK, so a stuck cell reads as absent, never as a mass.
      emitWeight(0, "ERR");
      // Then try to get it back. A DOUT stuck HIGH is what a powered-down
      // converter looks like, and the only way out is the reset sequence.
      // Reported first and recovered second, so a run that needed recovering
      // still leaves the ERR line in the log that says it happened.
      resetHx711();
    }
  }
}
