/*
 * Aurum - sorter firmware. Weight sensing + Servo A/B actuation.
 *
 * RUN ON HARDWARE. The Python side has opened a real serial port to an
 * attached Arduino, sent MOVE, and had it acknowledged. What no one has done
 * is watch a paddle: see docs/hardware.md, and scripts/bench_check.py.
 *
 * WARNING: the board on the bench is NOT flashed with this file. It banners
 * `SERVO_INIT B rest=90`, a string that appears nowhere in this repository,
 * so the running firmware is an older build. Reflash before trusting any
 * behaviour described here.
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

const uint8_t  PROTOCOL_VERSION   = 1;
const unsigned long SAMPLE_INTERVAL_MS = 100;   // 10 Hz, the HX711 rate
const unsigned long READY_TIMEOUT_MS   = 500;

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

  // Attach and park at rest before anything else, so a reset cannot leave a
  // paddle out in the stream.
  servoA.attach(PIN_SERVO_A);
  servoB.attach(PIN_SERVO_B);
  servoA.write(restAngle);
  servoB.write(restAngle);
  inputLine.reserve(64);
}

// ---------------------------------------------------------------- HX711

bool waitReady(unsigned long timeoutMs) {
  unsigned long start = millis();
  while (digitalRead(PIN_DOUT) == HIGH) {
    if (millis() - start > timeoutMs) return false;
  }
  return true;
}

long readRaw() {
  long value = 0;
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

// Blocking on purpose. One paddle moves at a time, weight sampling pauses for
// holdMs, and there is nothing else this board should be doing meanwhile.
void push(Servo &servo) {
  servo.write(pushAngle);
  delay(holdMs);
  servo.write(restAngle);
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
    servoA.write(restAngle);
    servoB.write(restAngle);
    ack(id, "");
    return;
  }

  if (verb == "MOVE") {
    String target = field(line, 2);
    String id     = field(line, 4);
    if (id.length() == 0) { err(id, "BAD_FRAME"); return; }

    if (alreadyDone(id)) { ack(id, "DUP"); return; }   // never move twice

    if (target == "A")      { remember(id); push(servoA); ack(id, ""); }
    else if (target == "B") { remember(id); push(servoB); ack(id, ""); }
    else                    { err(id, "BAD_TARGET"); }   // C has no servo
    return;
  }

  err(field(line, 2), "BAD_FRAME");
}

// ------------------------------------------------------------------ loop

unsigned long lastSample = 0;

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

  if (millis() - lastSample >= SAMPLE_INTERVAL_MS) {
    lastSample = millis();
    if (waitReady(READY_TIMEOUT_MS)) {
      emitWeight(readRaw(), "OK");
    } else {
      // Say so rather than repeating the last good value: Python drops any
      // line that is not OK, so a stuck cell reads as absent, never as a mass.
      emitWeight(0, "ERR");
    }
  }
}
