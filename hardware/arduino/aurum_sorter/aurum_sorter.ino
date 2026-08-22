/*
 * Aurum - the SIH demonstration sketch. HX711 weight + Servo A/B actuation.
 *
 * ONE board, ONE serial port, two frame types sharing it:
 *
 *   board -> host   W,1,<board_millis>,<raw_counts>,<status>      10 Hz
 *   host  -> board  AURUM/1 MOVE <A|B> <item_id> <command_id>
 *   host  -> board  AURUM/1 PING <command_id>
 *   host  -> board  AURUM/1 CFG <rest_deg> <push_deg> <hold_ms>
 *   board -> host   AURUM/1 ACK <command_id> [DUP]
 *   board -> host   AURUM/1 ERR <command_id> <code>
 *   board -> host   AURUM/1 PONG <command_id>
 *
 * Wiring, as built and bench-tested on 2026-08-22:
 *
 *   HX711 DOUT -> D2        Servo A signal -> D9
 *   HX711 SCK  -> D3        Servo B signal -> D10
 *   HX711 VCC/GND -> 5V/GND
 *
 * Servo power is an external 5 V / 3 A supply. Its ground is common with the
 * Arduino; its +5 V rail is NOT connected to the Arduino 5 V pin. An earlier
 * attempt at the other arrangement melted jumper wires. Do not change it.
 *
 * There is NO BIN C SERVO. Bin C is reached by this board doing nothing, which
 * is also what happens if the host crashes or the cable falls out.
 *
 * RAW COUNTS, never grams. Calibration lives in Python
 * (configs/calibration.yaml, written by `python -m app.calibrate`) because a
 * factor is measured data that belongs in version control next to the workflow
 * that produced it. A factor compiled into firmware is a number nobody can
 * check.
 *
 * The ACK is sent AFTER the stroke completes, so it means "the paddle finished
 * moving", not "the frame arrived". It is still not proof the paddle moved: a
 * stripped horn or a dead supply rail acknowledges exactly the same way.
 * Physical movement is established by watching it, and recorded in
 * docs/hardware.md.
 *
 * The weight-only sketch (../aurum_weight/) stays the one to CALIBRATE with:
 * it cannot move anything, so a bug in the weight path cannot swing a paddle
 * while your hand is on the pan.
 */

#include <Servo.h>

const uint8_t PIN_DOUT    = 2;
const uint8_t PIN_SCK     = 3;
const uint8_t PIN_SERVO_A = 9;
const uint8_t PIN_SERVO_B = 10;

const uint8_t PROTOCOL_VERSION = 1;
const unsigned long SAMPLE_INTERVAL_MS = 100;  // 10 Hz; the HX711 runs at 10 SPS
const unsigned long READY_TIMEOUT_MS   = 500;

// Bench values, overridable at runtime by a CFG frame so tuning the throw
// needs no reflash. Not validated mechanical geometry: no paddle has ever
// deflected an item, because there is no belt for an item to travel on.
int restAngle = 0;
int pushAngle = 90;
unsigned long holdMs = 700;

Servo servoA;
Servo servoB;

// The last few command ids acted on. An ACK lost on the wire must not let a
// resend swing the same paddle twice at whatever is now in front of it.
const uint8_t RECENT_SLOTS = 8;
String recentIds[RECENT_SLOTS];
uint8_t recentNext = 0;

String inputLine = "";
unsigned long lastSample = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_DOUT, INPUT);
  pinMode(PIN_SCK, OUTPUT);
  digitalWrite(PIN_SCK, LOW);
  inputLine.reserve(96);
  // Servos are deliberately NOT attached here. An unattached pin sends no
  // pulses, so nothing can twitch while the board boots or while the host is
  // still deciding whether it wants to actuate at all.
}

// ---------------------------------------------------------------- HX711 ----

// DOUT goes low when a conversion is ready.
bool waitReady(unsigned long timeoutMs) {
  unsigned long start = millis();
  while (digitalRead(PIN_DOUT) == HIGH) {
    if (millis() - start > timeoutMs) return false;
  }
  return true;
}

// One 24-bit two's-complement conversion, channel A gain 128.
long readRaw() {
  long value = 0;
  for (uint8_t i = 0; i < 24; i++) {
    digitalWrite(PIN_SCK, HIGH);
    delayMicroseconds(1);
    value = (value << 1) | digitalRead(PIN_DOUT);
    digitalWrite(PIN_SCK, LOW);
    delayMicroseconds(1);
  }
  // A 25th pulse selects channel A at gain 128 for the next conversion.
  digitalWrite(PIN_SCK, HIGH);
  delayMicroseconds(1);
  digitalWrite(PIN_SCK, LOW);
  delayMicroseconds(1);

  // Sign-extend 24 bits into 32.
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

void sampleWeight() {
  if (waitReady(READY_TIMEOUT_MS)) {
    emitWeight(readRaw(), "OK");
  } else {
    // Say so rather than repeating the last good value. The host drops any
    // line that is not OK, so a stuck cell reads as absent, never as a mass.
    emitWeight(0, "ERR");
  }
}

// -------------------------------------------------------------- commands ----

void reply(const char *verb, const String &id, const char *extra) {
  Serial.print("AURUM/1 ");
  Serial.print(verb);
  Serial.print(' ');
  Serial.print(id);
  if (extra != NULL) {
    Serial.print(' ');
    Serial.print(extra);
  }
  Serial.println();
}

bool alreadyActioned(const String &id) {
  for (uint8_t i = 0; i < RECENT_SLOTS; i++) {
    if (recentIds[i] == id) return true;
  }
  return false;
}

void remember(const String &id) {
  recentIds[recentNext] = id;
  recentNext = (recentNext + 1) % RECENT_SLOTS;
}

// One stroke: out, hold, back, release. Detaching afterwards stops the pulse
// train, so the horn is not fighting to hold position between items and the
// external supply is not carrying holding current all demonstration.
void stroke(Servo &servo, uint8_t pin) {
  servo.attach(pin);
  servo.write(restAngle);
  delay(60);
  servo.write(pushAngle);
  delay(holdMs);
  servo.write(restAngle);
  delay(300);
  servo.detach();
}

// `line` is one whitespace-separated frame, already trimmed.
void handleFrame(const String &line) {
  if (!line.startsWith("AURUM/1 ")) return;  // W frames are ours; noise is not

  String rest = line.substring(8);
  int firstSpace = rest.indexOf(' ');
  String verb = firstSpace < 0 ? rest : rest.substring(0, firstSpace);
  String args = firstSpace < 0 ? "" : rest.substring(firstSpace + 1);

  if (verb == "PING") {
    reply("PONG", args.length() ? args : "-", NULL);
    return;
  }

  if (verb == "CFG") {
    // CFG <rest_deg> <push_deg> <hold_ms>
    int s1 = args.indexOf(' ');
    int s2 = s1 < 0 ? -1 : args.indexOf(' ', s1 + 1);
    if (s1 < 0 || s2 < 0) {
      reply("ERR", "-", "BAD_FRAME");
      return;
    }
    restAngle = args.substring(0, s1).toInt();
    pushAngle = args.substring(s1 + 1, s2).toInt();
    holdMs    = (unsigned long) args.substring(s2 + 1).toInt();
    reply("ACK", "CFG", NULL);
    return;
  }

  if (verb != "MOVE") {
    reply("ERR", "-", "BAD_VERB");
    return;
  }

  // MOVE <A|B> <item_id> <command_id>
  int s1 = args.indexOf(' ');
  int s2 = s1 < 0 ? -1 : args.indexOf(' ', s1 + 1);
  if (s1 < 0 || s2 < 0) {
    reply("ERR", "-", "BAD_FRAME");
    return;
  }
  String target    = args.substring(0, s1);
  String commandId = args.substring(s2 + 1);
  commandId.trim();

  if (alreadyActioned(commandId)) {
    // Idempotent: acknowledge without moving anything a second time.
    reply("ACK", commandId, "DUP");
    return;
  }

  if (target == "A") {
    remember(commandId);
    stroke(servoA, PIN_SERVO_A);
    reply("ACK", commandId, NULL);
  } else if (target == "B") {
    remember(commandId);
    stroke(servoB, PIN_SERVO_B);
    reply("ACK", commandId, NULL);
  } else {
    // Includes "C". There is no Servo C, and inventing one here would be the
    // single most misleading thing this firmware could do.
    reply("ERR", commandId, "BAD_TARGET");
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = (char) Serial.read();
    if (c == '\n') {
      inputLine.trim();
      if (inputLine.length() > 0) handleFrame(inputLine);
      inputLine = "";
    } else if (c != '\r') {
      // A frame longer than this is malformed by definition; dropping the
      // overflow stops a runaway sender from exhausting RAM mid-demo.
      if (inputLine.length() < 90) inputLine += c;
    }
  }
}

void loop() {
  readSerial();
  if (millis() - lastSample >= SAMPLE_INTERVAL_MS) {
    lastSample = millis();
    sampleWeight();
  }
}
