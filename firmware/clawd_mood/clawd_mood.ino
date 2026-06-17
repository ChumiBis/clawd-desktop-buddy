/*
 * clawd-mood — ESP32-C3 Super Mini + ST7789 1.54" 240x240
 */
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>
#include <ArduinoJson.h>

#define TFT_CS  4
#define TFT_DC  1
#define TFT_RST 2
#define TFT_BLK 3

#define DISP_W 240
#define DISP_H 240

#define DONE_REVERT_MS    3000UL          // Done → Idle after 3s
#define SLEEP_IDLE_MS     (5UL*60*1000)   // 5 minutes

// Eye geometry (shared with clawd-mochi)
#define EYE_W   30
#define EYE_H   60
#define EYE_GAP 120
#define EYE_OX  0
#define EYE_OY  40

// Colors
uint16_t C_GREEN, C_YELLOW, C_BUSY, C_DARKBG, C_MUTED;
#define C_WHITE ST77XX_WHITE
#define C_BLACK ST77XX_BLACK

uint16_t bgColor = 0;   // current load color (green/orange/red), set from daemon "color"
bool blinkBg = false;   // true while a session is waiting for my confirmation

Adafruit_ST7789 tft = Adafruit_ST7789(TFT_CS, TFT_DC, TFT_RST);

enum Mood {
  MOOD_IDLE, MOOD_THINKING, MOOD_WORKING, MOOD_WAITING,
  MOOD_DONE, MOOD_ERROR, MOOD_SLEEPING, MOOD_UNKNOWN
};

Mood currentMood = MOOD_IDLE;
unsigned long lastEventMs = 0;
unsigned long doneEnteredMs = 0;
bool moodDirty = true;
String serialBuf;
int workingCount = 0;   // concurrent running sessions = working+error+waiting (from daemon "count")

// Off-screen framebuffer (double buffering): every frame is composed here, then
// pushed to the panel in a single drawRGBBitmap blit. The on-glass pixels are
// never cleared-then-redrawn, so there is no flicker on wiggle/blink/state changes.
GFXcanvas16 canvas(DISP_W, DISP_H);
inline void present() { tft.drawRGBBitmap(0, 0, canvas.getBuffer(), DISP_W, DISP_H); }

void initColors() {
  C_GREEN  = tft.color565(0, 150, 70);     // 0 running — idle / free
  C_YELLOW = tft.color565(255, 200, 0);    // 1 running — yellow
  C_BUSY   = tft.color565(218, 17, 0);     // >=2 running — original device orange (#DA1100)
  C_DARKBG = tft.color565(10, 12, 16);     // blink "off" / dim
  C_MUTED  = tft.color565(90, 88, 86);
  bgColor  = C_GREEN;                       // boot: nothing running
}

// Map the daemon's color name to RGB565. Unknown/absent -> keep current.
uint16_t colorToRGB(const char* name) {
  if (!name) return bgColor;
  if (!strcmp(name, "green"))  return C_GREEN;   // load token -> RGB; tokens stay green/orange/red
  if (!strcmp(name, "orange")) return C_YELLOW;  // 1 running  -> yellow
  if (!strcmp(name, "red"))    return C_BUSY;    // >=2 running -> device orange
  return bgColor;
}

// Linear-interpolate two RGB565 colors (k: 0 -> a, 1 -> b). GFX has no alpha,
// so the sleeping "zzz" fades by lerping its color toward the background.
uint16_t lerp565(uint16_t a, uint16_t b, float k) {
  uint8_t ar = ((a >> 11) & 0x1F) << 3, ag = ((a >> 5) & 0x3F) << 2, ab = (a & 0x1F) << 3;
  uint8_t br = ((b >> 11) & 0x1F) << 3, bg = ((b >> 5) & 0x3F) << 2, bb = (b & 0x1F) << 3;
  return tft.color565(ar + (br - ar) * k, ag + (bg - ag) * k, ab + (bb - ab) * k);
}

// Background to fill this frame. When blinkBg (a session is waiting), the load
// color alternates with a dim color on a ~1.2s cycle (600ms on / 600ms off).
uint16_t activeBg() {
  if (!blinkBg) return bgColor;
  return ((millis() / 600) % 2 == 0) ? bgColor : C_DARKBG;
}

inline int16_t eyeLX(int16_t ox) {
  return (DISP_W - (EYE_W * 2 + EYE_GAP)) / 2 + EYE_OX + ox;
}
inline int16_t eyeRX(int16_t ox) { return eyeLX(ox) + EYE_W + EYE_GAP; }
inline int16_t eyeY()            { return (DISP_H - EYE_H) / 2 - EYE_OY; }
inline int16_t eyeCY()           { return eyeY() + EYE_H / 2; }

// blink: 0 = open, 1 = half-closed, 2 = closed
void drawNormalEyes(int16_t ox = 0, uint8_t blink = 0) {
  canvas.fillScreen(bgColor);
  const int16_t lx = eyeLX(ox), rx = eyeRX(ox), ey = eyeY();
  int16_t h;
  switch (blink) {
    case 0:  h = EYE_H;     break;
    case 1:  h = EYE_H / 2; break;
    default: h = 6;         break;
  }
  int16_t y = ey + (EYE_H - h) / 2;
  canvas.fillRect(lx, y, EYE_W, h, C_BLACK);
  canvas.fillRect(rx, y, EYE_W, h, C_BLACK);
  present();
}

void drawChevron(int16_t cx, int16_t cy, int16_t arm, int16_t reach,
                 uint8_t thk, bool rightFacing, uint16_t col) {
  for (int8_t t = -(int8_t)thk; t <= (int8_t)thk; t++) {
    if (rightFacing) {
      canvas.drawLine(cx - reach/2, cy - arm + t, cx + reach/2, cy + t,      col);
      canvas.drawLine(cx + reach/2, cy + t,       cx - reach/2, cy + arm + t, col);
    } else {
      canvas.drawLine(cx + reach/2, cy - arm + t, cx - reach/2, cy + t,      col);
      canvas.drawLine(cx - reach/2, cy + t,       cx + reach/2, cy + arm + t, col);
    }
  }
}

void drawSquishEyes(bool closed = false) {
  canvas.fillScreen(bgColor);
  const int16_t lx = eyeLX(0), rx = eyeRX(0), cy = eyeCY();
  const int16_t arm   = EYE_H / 2;
  const int16_t reach = EYE_W / 2;
  const int16_t lcx   = lx + EYE_W / 2;
  const int16_t rcx   = rx + EYE_W / 2;
  if (!closed) {
    drawChevron(lcx, cy, arm, reach, 10, true,  C_BLACK);
    drawChevron(rcx, cy, arm, reach, 10, false, C_BLACK);
  } else {
    canvas.fillRect(lx, cy - 5, EYE_W, 10, C_BLACK);
    canvas.fillRect(rx, cy - 5, EYE_W, 10, C_BLACK);
  }
  present();
}

const char* moodName(Mood m) {
  switch (m) {
    case MOOD_IDLE:     return "idle";
    case MOOD_THINKING: return "thinking";
    case MOOD_WORKING:  return "working";
    case MOOD_WAITING:  return "waiting";
    case MOOD_DONE:     return "done";
    case MOOD_ERROR:    return "error";
    case MOOD_SLEEPING: return "sleeping";
    default:            return "unknown";
  }
}

Mood parseMood(const char* s) {
  if (!s) return MOOD_UNKNOWN;
  if (!strcmp(s, "idle"))     return MOOD_IDLE;
  if (!strcmp(s, "thinking")) return MOOD_THINKING;
  if (!strcmp(s, "working"))  return MOOD_WORKING;
  if (!strcmp(s, "waiting"))  return MOOD_WAITING;
  if (!strcmp(s, "done"))     return MOOD_DONE;
  if (!strcmp(s, "error"))    return MOOD_ERROR;
  if (!strcmp(s, "sleeping")) return MOOD_SLEEPING;
  return MOOD_UNKNOWN;
}

void setMood(Mood m) {
  if (m == MOOD_UNKNOWN || m == currentMood) return;
  currentMood = m;
  moodDirty = true;
  if (m == MOOD_DONE) doneEnteredMs = millis();
}

void tickMoodMachine() {
  unsigned long now = millis();
  if (currentMood == MOOD_DONE && now - doneEnteredMs > DONE_REVERT_MS) {
    setMood(MOOD_IDLE);
  }
  if (currentMood != MOOD_SLEEPING && now - lastEventMs > SLEEP_IDLE_MS) {
    setMood(MOOD_SLEEPING);
  }
}

void handleLine(const String& line) {
  if (!line.length()) return;
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    Serial.print("[warn] bad json: "); Serial.println(line);
    return;
  }
  const char* s = doc["state"] | "";
  Mood next = parseMood(s);
  if (next == MOOD_UNKNOWN) {
    Serial.print("[warn] unknown state: "); Serial.println(s);
    return;
  }
  int newCount = doc["count"] | 0;
  if (newCount != workingCount) {
    workingCount = newCount;
    moodDirty = true;   // count changed → force a redraw even if mood is unchanged
  }
  uint16_t newBg = colorToRGB(doc["color"] | "");
  if (newBg != bgColor) { bgColor = newBg; moodDirty = true; }
  bool newBlink = doc["blink"] | false;
  if (newBlink != blinkBg) { blinkBg = newBlink; moodDirty = true; }
  setMood(next);
  lastEventMs = millis();
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handleLine(serialBuf);
      serialBuf = "";
    } else if (c != '\r') {
      serialBuf += c;
      if (serialBuf.length() > 256) serialBuf = "";
    }
  }
}

// ── Per-mood renderers ──────────────────────────────────────────
// Each renderer is called every frame (~33fps). They use a static
// frame counter for animation. They redraw only when needed.

// ── Bottom running-count: one icon per concurrent session ────────
// The count is drawn as N identical icons (not a digit), so it never collides
// with any "busy" animation: 1 session = 1 icon, 3 = 3 icons. Past 6 (rare) we
// fall back to one icon + a number to stay countable.
//
// Icon = a vertical bar (equalizer / load-meter style), gently pulsing. To
// switch the motif, replace drawIcon's body — e.g. a fillCircle for a dot.
void drawIcon(int16_t x, int16_t cy, int i) {
  int16_t h = 20 + (int16_t)(sinf(millis() / 280.0f + i * 1.2f) * 4);  // ~16-24px pulse
  canvas.fillRoundRect(x - 5, cy + 12 - h, 10, h, 3, C_BLACK);         // bottoms aligned
}

void drawCountIcons(int16_t cy) {
  int n = workingCount;
  if (n < 1) return;
  if (n <= 6) {
    int16_t gap = (n <= 4) ? 30 : 26;             // tighten spacing past 4 so it stays centered
    int16_t x0 = 120 - (n - 1) * gap / 2;
    for (int i = 0; i < n; i++) drawIcon(x0 + i * gap, cy, i);
  } else {
    drawIcon(104, cy, 0);
    char buf[8];
    snprintf(buf, sizeof(buf), "%d", n);
    canvas.setTextColor(C_BLACK);
    canvas.setTextSize(3);
    canvas.setCursor(122, cy - 10);
    canvas.print(buf);
  }
}

void drawIdle() {
  static uint8_t step = 0;
  static unsigned long lastWiggle = 0;
  static unsigned long blinkStart = 0;   // 0 = not blinking
  static unsigned long nextBlinkAt = 0;
  static uint8_t lastPhase = 0;
  unsigned long now = millis();

  if (nextBlinkAt == 0) nextBlinkAt = now + 5000;

  // Blink animation: half-closed → closed → half-closed → open, ~280ms total
  uint8_t phase = 0;
  if (blinkStart) {
    unsigned long el = now - blinkStart;
    if      (el < 80)  phase = 1;
    else if (el < 200) phase = 2;
    else if (el < 280) phase = 1;
    else { blinkStart = 0; phase = 0; }
  }

  if (!blinkStart && now >= nextBlinkAt) {
    blinkStart = now;
    nextBlinkAt = now + 5000 + (now % 2500);  // 5–7.5s
    phase = 1;
  }

  bool blinking = phase != 0;
  bool wiggleDue = now - lastWiggle > 800;
  bool phaseEdge = phase != lastPhase;
  if (moodDirty || phaseEdge || (wiggleDue && !blinking)) {
    const int16_t offs[] = {0, -4, 0, 4, 0};
    drawNormalEyes(offs[step % 5], phase);
    if (wiggleDue && !blinking) {
      step++;
      lastWiggle = now;
    }
    lastPhase = phase;
    moodDirty = false;
  }
}

void drawThinking() {
  static uint8_t phase = 0;
  static unsigned long lastStep = 0;
  unsigned long now = millis();
  // Loop: up → left → right → center, 600ms each
  if (moodDirty || now - lastStep > 600) {
    canvas.fillScreen(bgColor);
    int16_t lx = eyeLX(0), rx = eyeRX(0), ey = eyeY();
    int16_t dx = 0, dy = 0;
    switch (phase % 4) {
      case 0: dx = 0;  dy = -8; break; // up
      case 1: dx = -8; dy = 0;  break; // left
      case 2: dx = 8;  dy = 0;  break; // right
      case 3: dx = 0;  dy = 0;  break; // center
    }
    canvas.fillRect(lx + dx, ey + dy, EYE_W, EYE_H, C_BLACK);
    canvas.fillRect(rx + dx, ey + dy, EYE_W, EYE_H, C_BLACK);
    drawCountIcons(196);  // (legacy: daemon no longer sends "thinking")
    present();
    phase++;
    lastStep = now;
    moodDirty = false;
  }
}

// Spinning spiral ("dizzy") eye, drawn as overlapping dots along an
// Archimedean spiral. Used when >=2 sessions run (overloaded = tired).
void drawSpiralEye(int16_t cx, int16_t cy, float rot) {
  for (float a = 0; a < PI * 3.6f; a += 0.13f) {
    float r = 2 + a * 2.3f;
    canvas.fillCircle(cx + (int16_t)(cosf(a + rot) * r),
                      cy + (int16_t)(sinf(a + rot) * r), 2, C_BLACK);
  }
}

void drawWorking() {
  static unsigned long lastStep = 0;
  unsigned long now = millis();
  bool dizzy = workingCount >= 2;            // 1 task = focused; 2+ = dizzy/tired
  unsigned long interval = dizzy ? 60 : 300; // spiral spins smoothly; jitter is slow
  if (moodDirty || now - lastStep > interval) {
    canvas.fillScreen(bgColor);
    if (dizzy) {
      float rot = now / 600.0f;
      drawSpiralEye(eyeLX(0) + EYE_W / 2, eyeCY(), rot);
      drawSpiralEye(eyeRX(0) + EYE_W / 2, eyeCY(), rot + 0.6f);
    } else {
      int16_t lx = eyeLX(0), rx = eyeRX(0), ey = eyeY();
      int16_t jx = (now / 100) % 3 - 1;      // -1, 0, 1 focused twitch
      canvas.fillRect(lx + jx, ey, EYE_W, EYE_H, C_BLACK);
      canvas.fillRect(rx - jx, ey, EYE_W, EYE_H, C_BLACK);
    }
    drawCountIcons(196);
    present();
    lastStep = now;
    moodDirty = false;
  }
}

void drawWaiting() {
  static uint8_t step = 0;
  static unsigned long lastStep = 0;
  unsigned long now = millis();
  if (moodDirty || now - lastStep > 250) {
    canvas.fillScreen(activeBg());   // blinks: load color <-> dim
    int16_t lx = eyeLX(0), rx = eyeRX(0), ey = eyeY();
    int16_t bounce = (step % 4 < 2) ? -6 : 6;
    // Slightly bigger eyes for "wide open" look
    int16_t eh = EYE_H + 6;
    canvas.fillRect(lx, ey + bounce - 3, EYE_W, eh, C_BLACK);
    canvas.fillRect(rx, ey + bounce - 3, EYE_W, eh, C_BLACK);
    drawCountIcons(196);   // running count icons (waiting is counted; no more "?")
    present();
    step++;
    lastStep = now;
    moodDirty = false;
  }
}

void drawDone() {
  static unsigned long enteredAt = 0;
  static uint8_t phase = 0;
  unsigned long now = millis();
  if (moodDirty) {
    enteredAt = now;
    phase = 0;
    moodDirty = false;
  }
  unsigned long elapsed = now - enteredAt;
  // First 1000ms: alternate squish/closed every 150ms for celebration
  // After 1000ms: steady squish
  if (elapsed < 1000) {
    uint8_t newPhase = (elapsed / 150) % 2;
    if (newPhase != phase) {
      drawSquishEyes(newPhase == 1);
      phase = newPhase;
    }
  } else if (phase != 99) {
    drawSquishEyes(false);
    phase = 99;
  }
}

void drawError() {
  static unsigned long lastStep = 0;
  unsigned long now = millis();
  if (moodDirty || now - lastStep > 80) {
    canvas.fillScreen(bgColor);
    int16_t lx = eyeLX(0), rx = eyeRX(0), ey = eyeY();
    // Jittery, asymmetric: random small offset, left eye lower than right
    int16_t jx = ((now / 80) * 17) % 5 - 2;
    int16_t jy = ((now / 80) * 31) % 5 - 2;
    canvas.fillRect(lx + jx, ey + 6 + jy, EYE_W, EYE_H - 6, C_BLACK);
    canvas.fillRect(rx - jx, ey - 6 + jy, EYE_W, EYE_H - 6, C_BLACK);
    drawCountIcons(196);   // error is counted as running → show the count
    present();
    lastStep = now;
    moodDirty = false;
  }
}

void drawSleeping() {
  static unsigned long lastStep = 0;
  unsigned long now = millis();
  if (moodDirty || now - lastStep > 90) {       // ~11fps: slow, smooth rise
    canvas.fillScreen(bgColor);
    int16_t lx = eyeLX(0), rx = eyeRX(0), cy = eyeCY();
    // Closed eyes with a gentle breathing bob
    int16_t br = (int16_t)(sinf(now / 700.0f) * 1.5f);
    canvas.fillRect(lx, cy - 2 + br, EYE_W, 4, C_BLACK);
    canvas.fillRect(rx, cy - 2 + br, EYE_W, 4, C_BLACK);
    // Three z's rising from the bottom-center, fading into the bg near the top
    const unsigned long T = 2400;
    for (int i = 0; i < 3; i++) {
      float p = ((now + i * (T / 3)) % T) / (float)T;     // 0 -> 1 lifetime
      uint8_t sz = (p < 0.45f) ? 2 : 3;                   // small z then big Z
      int16_t x = 120 - 3 * sz + (int16_t)(sinf(p * PI) * 8);
      int16_t y = 205 - (int16_t)(p * 60);
      float k = (p < 0.7f) ? 0.0f : (p - 0.7f) / 0.3f;    // fade to bg in last 30%
      canvas.setTextColor(lerp565(C_BLACK, bgColor, k));
      canvas.setTextSize(sz);
      canvas.setCursor(x, y);
      canvas.print((p < 0.45f) ? "z" : "Z");
    }
    present();
    lastStep = now;
    moodDirty = false;
  }
}

void renderMood() {
  switch (currentMood) {
    case MOOD_IDLE:     drawIdle(); break;
    case MOOD_THINKING: drawThinking(); break;
    case MOOD_WORKING:  drawWorking(); break;
    case MOOD_WAITING:  drawWaiting(); break;
    case MOOD_DONE: drawDone(); break;
    case MOOD_ERROR: drawError(); break;
    case MOOD_SLEEPING: drawSleeping(); break;
    default: break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(TFT_BLK, OUTPUT); digitalWrite(TFT_BLK, HIGH);
  SPI.begin(8, -1, 10, TFT_CS);   // SCK=8, MOSI=10 (ESP32-C3 pin remap)
  tft.setSPISpeed(40000000);
  tft.init(DISP_W, DISP_H);
  tft.setRotation(1);
  initColors();
  canvas.fillScreen(bgColor);
  present();
  lastEventMs = millis();
}

void loop() {
  pollSerial();
  tickMoodMachine();
  renderMood();
  delay(30);
}
