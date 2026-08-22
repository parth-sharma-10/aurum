/*
 * Aurum - HX711 weight sensing. Phase 5.
 *
 * WEIGHT ONLY. There is deliberately no servo code in this sketch. Servo A and
 * Servo B were bench-tested separately and belong to Phase 7; keeping actuation
 * out of the sketch that runs during weighing means a bug here cannot move
 * anything physical.
 *
 * Wiring, as built and tested on 2026-08-22:
 *
 *   HX711 DOUT -> Arduino D2
 *   HX711 SCK  -> Arduino D3
 *   HX711 VCC  -> Arduino 5V
 *   HX711 GND  -> Arduino GND
 *
 * Servo power comes from an external AKSHA 5V/3A supply whose ground is common
 * with the Arduino. The external +5V rail is NOT connected to the Arduino +5V
 * pin, and this sketch does not touch the servo pins at all.
 *
 * Protocol, one line per sample:
 *
 *   W,<version>,<board_millis>,<raw_counts>,<status>
 *
 * for example:  W,1,10432,-261605,OK
 *
 * RAW COUNTS, never grams. Calibration lives in Python
 * (configs/calibration.yaml, written by `python -m app.calibrate`) because a
 * factor is measured data that belongs in version control next to the workflow
 * that produced it and the second known mass that verified it. A factor
 * compiled into firmware is a number nobody can check.
 *
 * A failed read emits status ERR with a zero count. Python drops any line that
 * is not OK rather than treating that zero as a mass.
 *
 * No external library: the HX711 is two pins and a shift register, and this is
 * fewer lines than the dependency would be.
 */

const uint8_t PIN_DOUT = 2;
const uint8_t PIN_SCK  = 3;

const uint8_t PROTOCOL_VERSION = 1;
const unsigned long SAMPLE_INTERVAL_MS = 100;  // 10 Hz; the HX711 runs at 10 SPS
const unsigned long READY_TIMEOUT_MS   = 500;

void setup() {
  // 115200, matching conveyor.arduino.baudrate and the sorter sketch.
  // app/calibrate.py opens the port at that rate, so a 9600 board here reads
  // as garbage rather than as silence - the more confusing failure.
  Serial.begin(115200);
  pinMode(PIN_DOUT, INPUT);
  pinMode(PIN_SCK, OUTPUT);
  digitalWrite(PIN_SCK, LOW);
}

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

void emit(long counts, const char *status) {
  Serial.print("W,");
  Serial.print(PROTOCOL_VERSION);
  Serial.print(',');
  Serial.print(millis());
  Serial.print(',');
  Serial.print(counts);
  Serial.print(',');
  Serial.println(status);
}

void loop() {
  if (waitReady(READY_TIMEOUT_MS)) {
    emit(readRaw(), "OK");
  } else {
    // Say so rather than repeating the last good value. Python drops any line
    // that is not OK, so a stuck cell reads as absent, never as a mass.
    emit(0, "ERR");
  }
  delay(SAMPLE_INTERVAL_MS);
}
