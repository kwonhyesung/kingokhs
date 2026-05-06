#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDMouse.h"

USBHIDKeyboard Keyboard;
USBHIDMouse Mouse;

// 버퍼 사이즈 최대로 확장
#define SERIAL_BUFFER_SIZE 1024
#define COMMAND_BUFFER_SIZE 512

void setup() {
  Keyboard.begin();
  Mouse.begin();
  USB.begin();
  Serial.begin(115200);
  
  // 시리얼 버퍼 사이즈 최대로 설정
  Serial.setRxBufferSize(SERIAL_BUFFER_SIZE);
}

// 문자열에서 특정 인덱스의 인자를 추출하는 함수
String getValue(String data, char separator, int index) {
  int found = 0;
  int strIndex[] = { 0, -1 };
  int maxIndex = data.length() - 1;
  for (int i = 0; i <= maxIndex && found <= index; i++) {
    if (data.charAt(i) == separator || i == maxIndex) {
      found++;
      strIndex[0] = strIndex[1] + 1;
      strIndex[1] = (i == maxIndex) ? i + 1 : i;
    }
  }
  return found > index ? data.substring(strIndex[0], strIndex[1]) : "";
}

// 특수 키 이름 -> 키코드 변환 함수
uint8_t getSpecialKey(String keyName) {
  if (keyName == "f1") return KEY_F1;
  if (keyName == "f2") return KEY_F2;
  if (keyName == "f3") return KEY_F3;
  if (keyName == "f4") return KEY_F4;
  if (keyName == "f5") return KEY_F5;
  if (keyName == "f6") return KEY_F6;
  if (keyName == "f7") return KEY_F7;
  if (keyName == "f8") return KEY_F8;
  if (keyName == "f9") return KEY_F9;
  if (keyName == "f10") return KEY_F10;
  if (keyName == "f11") return KEY_F11;
  if (keyName == "f12") return KEY_F12;
  if (keyName == "f13") return KEY_F13;
  if (keyName == "f14") return KEY_F14;
  if (keyName == "f15") return KEY_F15;
  if (keyName == "f16") return KEY_F16;
  if (keyName == "f17") return KEY_F17;
  if (keyName == "f18") return KEY_F18;
  if (keyName == "f19") return KEY_F19;
  if (keyName == "f20") return KEY_F20;
  if (keyName == "f21") return KEY_F21;
  if (keyName == "f22") return KEY_F22;
  if (keyName == "f23") return KEY_F23;
  if (keyName == "f24") return KEY_F24;
  if (keyName == "up") return KEY_UP_ARROW;
  if (keyName == "down") return KEY_DOWN_ARROW;
  if (keyName == "left") return KEY_LEFT_ARROW;
  if (keyName == "right") return KEY_RIGHT_ARROW;
  if (keyName == "enter") return KEY_RETURN;
  if (keyName == "esc") return KEY_ESC;
  if (keyName == "tab") return KEY_TAB;
  if (keyName == "backspace") return KEY_BACKSPACE;
  if (keyName == "page up") return KEY_PAGE_UP;
  if (keyName == "page down") return KEY_PAGE_DOWN;
  if (keyName == "home") return KEY_HOME;
  if (keyName == "end") return KEY_END;
  if (keyName == "insert") return KEY_INSERT;
  if (keyName == "delete") return KEY_DELETE;
  if (keyName == "caps lock") return KEY_CAPS_LOCK;
  if (keyName == "scroll lock") return KEY_SCROLL_LOCK;
  if (keyName == "print screen") return KEY_PRINT_SCREEN;
  if (keyName == "pause") return KEY_PAUSE;
  if (keyName == "menu") return KEY_MENU;
  if (keyName == "left gui") return KEY_LEFT_GUI;
  if (keyName == "right gui") return KEY_RIGHT_GUI;
  if (keyName == "left shift") return KEY_LEFT_SHIFT;
  if (keyName == "right shift") return KEY_RIGHT_SHIFT;
  if (keyName == "left ctrl") return KEY_LEFT_CTRL;
  if (keyName == "right ctrl") return KEY_RIGHT_CTRL;
  if (keyName == "left alt") return KEY_LEFT_ALT;
  if (keyName == "right alt") return KEY_RIGHT_ALT;
  if (keyName == "+") return KEY_KP_PLUS;
  if (keyName == "-") return KEY_KP_MINUS;
  // 숫자패드 *, /, ., 0-9는 기본 키보드 키로 대체
  return 0;
}

void loop() {
  if (Serial.available() > 0) {
    // 확장된 버퍼로 명령어 수신
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.length() == 0) return;
    
    // 명령어 길이 제한으로 버퍼 오버플로우 방지
    if (cmd.length() > COMMAND_BUFFER_SIZE) {
      cmd = cmd.substring(0, COMMAND_BUFFER_SIZE);
    }

    // --- 1. 단축 명령어 (사용자 최신 hardware_input.py 호환) ---
    if (cmd == "L") { Mouse.click(MOUSE_LEFT); }
    else if (cmd == "R") { Mouse.click(MOUSE_RIGHT); }
    else if (cmd == "RD") { Mouse.press(MOUSE_RIGHT); }
    else if (cmd == "RU") { Mouse.release(MOUSE_RIGHT); }
    else if (cmd == "LD") { Mouse.press(MOUSE_LEFT); }
    else if (cmd == "LU") { Mouse.release(MOUSE_LEFT); }
    else if (cmd == "SD") { Keyboard.press(KEY_LEFT_SHIFT); }
    else if (cmd == "SU") { Keyboard.release(KEY_LEFT_SHIFT); }
    else if (cmd == "CD") { Keyboard.press(KEY_LEFT_CTRL); }
    else if (cmd == "CU") { Keyboard.release(KEY_LEFT_CTRL); }
    else if (cmd == "AD") { Keyboard.press(KEY_LEFT_ALT); }
    else if (cmd == "AU") { Keyboard.release(KEY_LEFT_ALT); }
    else if (cmd == "SPC") { Keyboard.write(' '); }
    else if (cmd == "NUM") { Keyboard.write(KEY_NUM_LOCK); }
    else if (cmd == "RELEASE_ALL") { 
      Keyboard.releaseAll(); 
      Mouse.release(MOUSE_LEFT); 
      Mouse.release(MOUSE_RIGHT); 
      Mouse.release(MOUSE_MIDDLE); 
    }

    // --- 2. 파라미터형 명령어 ---
    else if (cmd.startsWith("M,")) { // M,x,y (마우스 이동)
      int x = getValue(cmd, ',', 1).toInt();
      int y = getValue(cmd, ',', 2).toInt();
      Mouse.move(x, y);
    }
    else if (cmd.startsWith("K,")) { // K,key (문자열 키 입력)
      String ks = getValue(cmd, ',', 1);
      if (ks.length() == 1) Keyboard.write(ks.charAt(0));
      else { uint8_t sk = getSpecialKey(ks); if (sk > 0) { Keyboard.press(sk); delayMicroseconds(50); Keyboard.release(sk); } }
    }
    else if (cmd.startsWith("KD,")) { // KD,key (키 다운)
      String ks = getValue(cmd, ',', 1);
      if (ks.length() == 1) Keyboard.press(ks.charAt(0));
      else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.press(sk); }
    }
    else if (cmd.startsWith("KU,")) { // KU,key (키 업)
      String ks = getValue(cmd, ',', 1);
      if (ks.length() == 1) Keyboard.release(ks.charAt(0));
      else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.release(sk); }
    }
    else if (cmd.startsWith("D:")) { // D:key (Python 호환, 키 다운)
      int colonIdx = cmd.indexOf(':', 2); // 두 번째 콜론 찾기 (속도 파라미터)
      if (colonIdx > 0) {
        String ks = cmd.substring(2, colonIdx);
        int delayUs = cmd.substring(colonIdx + 1).toInt(); // 마이크로초 단위
        if (delayUs < 10) delayUs = 10; // 최소 10마이크로초
        if (delayUs > 1000) delayUs = 1000; // 최대 1000마이크로초
        if (ks.length() == 1) Keyboard.press(ks.charAt(0));
        else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.press(sk); }
        delayMicroseconds(delayUs);
      } else {
        String ks = cmd.substring(2);
        if (ks.length() == 1) Keyboard.press(ks.charAt(0));
        else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.press(sk); }
      }
    }
    else if (cmd.startsWith("U:")) { // U:key (Python 호환, 키 업)
      int colonIdx = cmd.indexOf(':', 2); // 두 번째 콜론 찾기 (속도 파라미터)
      if (colonIdx > 0) {
        String ks = cmd.substring(2, colonIdx);
        if (ks.length() == 1) Keyboard.release(ks.charAt(0));
        else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.release(sk); }
      } else {
        String ks = cmd.substring(2);
        if (ks.length() == 1) Keyboard.release(ks.charAt(0));
        else { uint8_t sk = getSpecialKey(ks); if (sk > 0) Keyboard.release(sk); }
      }
    }
    else if (cmd.startsWith("W,")) { // W,val (마우스 휠)
      int v = getValue(cmd, ',', 1).toInt();
      Mouse.move(0, 0, v);
    }
  }
}
