# xLua 런타임 스크립트 교체 DLL — 작업지시서

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** msw.exe(MapleStory Worlds)에 주입되어 `lua_load`를 후킹하고, 특정 이름의 Lua 청크 내용을 rules.txt 규칙대로 치환하며 로그를 남기는 DLL + 인젝터를 만든다.

**Architecture:** `injector.exe`가 msw.exe를 폴링해 `hook.dll`을 원격 스레드로 주입한다. DLL 워커 스레드는 xlua.dll이 로드될 때까지 기다린 뒤 `.text`에서 바이트 시그니처로 `lua_load`를 찾아 MinHook으로 인라인 후킹한다. 후킹 함수는 원래 reader를 전부 소비해 청크를 모으고, 치환 후 자체 reader로 원본을 호출한다. xlua.dll export가 전부 난독화돼 있어 이름으로는 못 찾고, 시그니처는 `tools/find_sig.py`로 사전에 추출한다.

**Tech Stack:** C++17 / MSVC 14.34 (`cl.exe`, IDE 없음), Win32 API, MinHook v1.3.4, Python 3.14 + `pefile` + `capstone` (시그니처 추출용)

**Spec:** `C:\Users\kwon\Desktop\0416 (1)\docs\superpowers\specs\2026-09-19-xlua-runtime-hook-design.md`

## Global Constraints

- 프로젝트 위치: `C:\Users\kwon\Desktop\luahook\` (새 git 저장소). 이 저장소(0416)와 분리.
- 대상: `C:\Nexon\MapleStory Worlds\msw.exe`, xlua.dll = `C:\Nexon\MapleStory Worlds\msw_Data\Plugins\x86_64\xlua.dll`. **게임 파일은 읽기만 한다. 절대 수정/덮어쓰기 금지.**
- 64비트 빌드만. `vcvars64.bat` 경로: `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat`
- 새 의존성은 MinHook, pefile, capstone 셋뿐. 그 외 추가 금지.
- `hook.log`, `dump/`, `rules.txt`는 hook.dll과 같은 폴더 기준.
- 후킹 함수 안에서 예외가 게임으로 새면 안 됨 (전체 try/catch).
- 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 사용자 안내 문구는 한국어.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `tools/find_sig.py` | xlua.dll 정적 분석 → `lua_load` 시그니처 출력·검증 |
| `hook.cpp` | (a) 순수 로직: rules 파서, 치환, sanitize, 시그니처 스캐너 (b) DLL: DllMain, 워커, 후킹 함수, 로그. `SELFTEST` 정의 시 (a)만 + `main()` |
| `injector.cpp` | 프로세스 찾기 → 2초 대기 → LoadLibraryW 원격 호출 |
| `rules.txt` | 옵션 + 규칙 |
| `build.bat` | 빌드 / `test` 인자로 셀프테스트 |
| `minhook/` | 벤더링 (수정 금지) |
| `README.md` | 사용 순서 |

---

### Task 0: 프로젝트 골격

**Files:**
- Create: `C:\Users\kwon\Desktop\luahook\.gitignore`, `README.md`

- [ ] **Step 1: 폴더 + git 초기화**

```bash
mkdir -p /c/Users/kwon/Desktop/luahook && cd /c/Users/kwon/Desktop/luahook && git init -q
```

- [ ] **Step 2: .gitignore 작성**

```
out/
__pycache__/
*.obj
*.exp
*.lib
```

- [ ] **Step 3: README.md 작성**

```markdown
# luahook — xLua 런타임 스크립트 교체 (학습용)

## 사용 순서
1. `build.bat` 실행 → `out\hook.dll`, `out\injector.exe` 생성
2. `out\rules.txt` 편집 (형식은 파일 안 주석 참고)
3. **게임을 켜기 전에** `out\injector.exe` 실행 (관리자 권한 권장)
4. 게임 실행 → `out\hook.log`에 `hook ready` 확인
5. `out\dump\`에서 로드된 스크립트 원문 확인 → 규칙 작성 → 게임 재시작 + 3번부터 반복

## 게임 업데이트 후 `SIG_FAIL`이 뜨면
`python tools\find_sig.py` 재실행 → 출력된 시그니처를 `hook.cpp`의 `LUA_LOAD_SIG`에 붙이고 재빌드.

## 주의
넥슨 약관상 클라이언트 개조는 금지. 본인 학습/본인 월드 범위에서만.
```

- [ ] **Step 4: 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add -A && git commit -qm "chore: project skeleton

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 1: 시그니처 추출 스크립트 (스파이크)

**Files:**
- Create: `tools/find_sig.py`
- Modify (결과 기록): `C:\Users\kwon\Desktop\0416 (1)\docs\superpowers\specs\2026-09-19-xlua-runtime-hook-design.md` 10절

**Interfaces:**
- Produces: 표준출력에 `SIG: 48 89 5C 24 ?? ...` 한 줄 (Task 3의 `LUA_LOAD_SIG` 값) + 근거 RVA들. 종료코드 0 = 유일 매치 확인.

**배경 지식 (Lua 5.3 소스 구조):**
- `ldo.c`: `checkmode()`가 문자열 `"attempt to load a %s chunk (mode is '%s')"`를 사용. `f_parser()`가 `checkmode`를 호출(보통 인라인). `luaD_protectedparser()`가 `luaD_pcall(L, f_parser, ...)`로 **f_parser 주소를 lea로 넘김**.
- `lapi.c`: `lua_load()`가 `luaD_protectedparser()`를 **call**. 호출자는 이 하나.
- x64 PE의 `.pdata`(exception directory)에 모든 함수의 시작/끝 RVA가 있어 "이 주소가 속한 함수"를 정확히 알 수 있음.

- [ ] **Step 1: 패키지 설치 (스크래치 venv 아님 — 프로젝트용 그대로 사용)**

```bash
python -m pip install --quiet pefile capstone
```

- [ ] **Step 2: 스크립트 작성**

```python
# tools/find_sig.py — xlua.dll에서 lua_load 위치를 찾아 바이트 시그니처를 출력한다.
# 사슬: "attempt to load a" 문자열 → f_parser → (lea 참조) luaD_protectedparser → (call 참조) lua_load
import struct, sys
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_OP_IMM

PATH = r"C:\Nexon\MapleStory Worlds\msw_Data\Plugins\x86_64\xlua.dll"
SIG_LEN = 32  # lua_load 앞부분 몇 바이트를 시그니처로 쓸지

pe = pefile.PE(PATH)
base = pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()            # RVA로 인덱싱되는 전체 이미지
text = next(s for s in pe.sections if s.Name.startswith(b".text"))
t0, t1 = text.VirtualAddress, text.VirtualAddress + text.Misc_VirtualSize

# .pdata: (begin, end) 함수 범위 목록
funcs = sorted((e.struct.BeginAddress, e.struct.EndAddress)
               for e in pe.DIRECTORY_ENTRY_EXCEPTION)

def func_of(rva):
    for b, e in funcs:
        if b <= rva < e:
            return b, e
    raise SystemExit(f"no function contains RVA {rva:#x}")

def lea_refs(target):
    """target RVA를 lea r64,[rip+disp32]로 참조하는 명령 위치들"""
    hits = []
    for i in range(t0, t1 - 7):
        if img[i] in (0x48, 0x4C) and img[i+1] == 0x8D and (img[i+2] & 0xC7) == 0x05:
            disp = struct.unpack_from("<i", img, i + 3)[0]
            if i + 7 + disp == target:
                hits.append(i)
    return hits

def call_refs(target):
    """target RVA를 E8 rel32로 호출하는 명령 위치들"""
    hits = []
    for i in range(t0, t1 - 5):
        if img[i] == 0xE8:
            rel = struct.unpack_from("<i", img, i + 1)[0]
            if i + 5 + rel == target:
                hits.append(i)
    return hits

def containing_funcs(refs):
    return sorted({func_of(r)[0] for r in refs})

# 1. 문자열
needle = b"attempt to load a %s chunk"
s = img.find(needle)
assert s > 0 and img.find(needle, s + 1) < 0, "string not unique"
print(f"string        RVA {s:#x}")

# 2. 문자열을 참조하는 함수 (checkmode 또는 인라인된 f_parser)
F = containing_funcs(lea_refs(s))
assert len(F) == 1, f"string referenced from {len(F)} functions: {F}"
F = F[0]
# checkmode가 인라인되지 않았으면, 함수포인터로 쓰이는(lea 참조되는) 함수가 나올 때까지 호출자를 따라 올라간다
while not lea_refs(F):
    callers = containing_funcs(call_refs(F))
    assert len(callers) == 1, f"expected 1 caller of {F:#x}, got {callers}"
    F = callers[0]
print(f"f_parser      RVA {F:#x}")

# 3. f_parser 주소를 lea로 넘기는 함수 = luaD_protectedparser
P = containing_funcs(lea_refs(F))
assert len(P) == 1, f"f_parser lea-referenced from {P}"
P = P[0]
print(f"protectedparser RVA {P:#x}")

# 4. 그것을 call하는 함수 = lua_load
L = containing_funcs(call_refs(P))
assert len(L) == 1, f"protectedparser called from {L}"
L = L[0]
print(f"lua_load      RVA {L:#x}")

# 5. 검증: lua_load는 chunkname NULL일 때 "?"를 쓴다 → lea로 '?\0' 참조가 있어야 함
Lb, Le = func_of(L)
md = Cs(CS_ARCH_X86, CS_MODE_64); md.detail = True
has_q = False
for ins in md.disasm(bytes(img[Lb:Le]), Lb):
    for op in ins.operands:
        if op.type == X86_OP_MEM and op.mem.base == 0x29 and op.mem.disp:  # X86_REG_RIP == 0x29
            tgt = ins.address + ins.size + op.mem.disp
            if img[tgt:tgt+2] == b"?\x00":
                has_q = True
assert has_q, "lua_load sanity check failed: no '?' string reference"
print("sanity        OK ('?' reference found)")

# 6. 시그니처: 앞 SIG_LEN 바이트, RIP상대/큰 imm/call·jmp 목표는 ??
sig = []
for ins in md.disasm(bytes(img[Lb:Lb+SIG_LEN+16]), Lb):
    if ins.address - Lb >= SIG_LEN:
        break
    raw = list(ins.bytes)
    wild = set()
    if ins.disp_size and any(op.type == X86_OP_MEM and op.mem.base == 0x29 for op in ins.operands):
        wild |= set(range(ins.disp_offset, ins.disp_offset + ins.disp_size))
    if ins.imm_size and ins.imm_size >= 4:
        wild |= set(range(ins.imm_offset, ins.imm_offset + ins.imm_size))
    if ins.mnemonic in ("call", "jmp") or ins.mnemonic.startswith("j"):
        wild |= set(range(1, len(raw)))
    sig += ["??" if k in wild else f"{b:02X}" for k, b in enumerate(raw)]
sig_str = " ".join(sig)

# 7. 유일성 검증 (파일 전체)
pat = [None if x == "??" else int(x, 16) for x in sig]
def matches(buf, pat):
    n = len(pat); out = []
    for i in range(len(buf) - n + 1):
        if all(p is None or buf[i+k] == p for k, p in enumerate(pat)):
            out.append(i)
    return out
m = matches(img, pat)
assert m == [Lb], f"signature not unique: {[hex(x) for x in m]}"
print(f"SIG: {sig_str}")
print("unique        OK")
```

- [ ] **Step 3: 실행**

```bash
cd /c/Users/kwon/Desktop/luahook && python tools/find_sig.py
```
기대: 각 단계 RVA와 `SIG: ...` 줄, `unique OK`. assert가 터지면 그 단계에서 중단하고 **사용자에게 보고** (사슬이 끊긴 것). 스스로 다른 휴리스틱을 만들어 계속하지 않는다.

- [ ] **Step 4: 스펙 10절에 결과 기록**

스펙 파일의 `## 10. 시그니처 (스파이크 결과)` 아래 `_스파이크 완료 후 기록._`을 다음으로 교체 (값은 실제 출력으로):

```markdown
- xlua.dll SHA-256 앞 16자: `<certutil -hashfile ... SHA256 결과>`
- string RVA `0x...` → f_parser `0x...` → luaD_protectedparser `0x...` → lua_load `0x...`
- `LUA_LOAD_SIG = "<SIG 줄>"`
- 추출: `python tools/find_sig.py` (2026-09-19)
```

- [ ] **Step 5: 커밋 (두 저장소)**

```bash
cd /c/Users/kwon/Desktop/luahook && git add tools/find_sig.py && git commit -qm "feat: lua_load signature extractor

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
cd "/c/Users/kwon/Desktop/0416 (1)" && git add docs/superpowers/specs && git commit -qm "Spec: record lua_load signature

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: MinHook 벤더링

**Files:**
- Create: `minhook/include/MinHook.h`, `minhook/src/*.c`, `minhook/src/*.h`, `minhook/src/hde/*`, `minhook/LICENSE.txt`

- [ ] **Step 1: 클론 (태그 고정) 후 필요한 것만 복사**

```bash
cd /c/Users/kwon/Desktop/luahook && git clone -q --depth 1 --branch v1.3.4 https://github.com/TsudaKageyu/minhook.git /tmp/minhook \
 && mkdir -p minhook && cp -r /tmp/minhook/include /tmp/minhook/src /tmp/minhook/LICENSE.txt minhook/ && rm -rf /tmp/minhook \
 && ls minhook/src minhook/src/hde
```
기대: `buffer.c buffer.h hook.c trampoline.c trampoline.h hde/` 및 `hde/hde32.c hde64.c ...`

- [ ] **Step 2: 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add minhook && git commit -qm "chore: vendor MinHook v1.3.4 (MIT)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: hook.cpp 순수 로직 + 셀프테스트 (TDD)

**Files:**
- Create: `hook.cpp` (순수 로직 부분 + `#ifdef SELFTEST main`), `build.bat`

**Interfaces:**
- Produces (Task 4가 사용):
  ```cpp
  struct Rule { std::string name, find, replace; };
  struct Config { bool dump = true; bool log = true; std::vector<Rule> rules; };
  Config parse_rules(const std::string& text);
  size_t replace_all(std::string& s, const std::string& find, const std::string& rep);  // 치환 횟수 반환
  std::string sanitize(const char* chunkname);                                          // 파일명용
  std::vector<int> parse_sig(const char* sig);                                          // "48 8B ??" → {0x48,0x8B,-1}
  std::vector<size_t> find_sig(const unsigned char* hay, size_t n, const std::vector<int>& pat); // 매치 오프셋들
  ```

- [ ] **Step 1: build.bat 작성**

```bat
@echo off
setlocal
cd /d "%~dp0"
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if not exist out mkdir out

if "%1"=="test" (
  cl /nologo /EHsc /std:c++17 /DSELFTEST hook.cpp /Fe:out\selftest.exe /Foout\ || exit /b 1
  out\selftest.exe
  exit /b %errorlevel%
)

cl /nologo /O2 /EHsc /std:c++17 /LD hook.cpp minhook\src\*.c minhook\src\hde\*.c /I minhook\include /Fe:out\hook.dll /Foout\ || exit /b 1
cl /nologo /O2 /EHsc /std:c++17 injector.cpp /Fe:out\injector.exe /Foout\ || exit /b 1
copy /Y rules.txt out\ >nul
echo BUILD OK
```

- [ ] **Step 2: 실패하는 셀프테스트부터 작성 — hook.cpp 초안 (선언 + main만, 구현은 비움)**

```cpp
// hook.cpp — xlua.dll의 lua_load를 후킹해 Lua 청크를 치환한다.
// /DSELFTEST 로 빌드하면 순수 로직만 콘솔 테스트한다.
#include <string>
#include <vector>
#include <cstdio>
#include <cstring>
#include <cassert>

struct Rule { std::string name, find, replace; };
struct Config { bool dump = true; bool log = true; std::vector<Rule> rules; };

Config parse_rules(const std::string& text);
size_t replace_all(std::string& s, const std::string& find, const std::string& rep);
std::string sanitize(const char* chunkname);
std::vector<int> parse_sig(const char* sig);
std::vector<size_t> find_sig(const unsigned char* hay, size_t n, const std::vector<int>& pat);

#ifdef SELFTEST
int main() {
    // rules 파서
    Config c = parse_rules("\xEF\xBB\xBF# c\r\n\r\ndump=0\nlog=1\nmain.lua|a\\nb|x\\\\y\nbad line\n");
    assert(!c.dump && c.log);
    assert(c.rules.size() == 1);
    assert(c.rules[0].name == "main.lua");
    assert(c.rules[0].find == "a\nb");
    assert(c.rules[0].replace == "x\\y");
    assert(parse_rules("").dump == true);  // 기본값

    // 치환
    std::string s = "aXbXc";
    assert(replace_all(s, "X", "YY") == 2 && s == "aYYbYYc");
    assert(replace_all(s, "Z", "q") == 0 && s == "aYYbYYc");
    s = "aa"; assert(replace_all(s, "a", "") == 2 && s.empty());

    // sanitize
    assert(sanitize("@ui/shop:1.lua") == "ui_shop_1.lua");
    assert(sanitize("=[C]") == "[C]");
    assert(sanitize(nullptr) == "(null)");

    // 시그니처
    std::vector<int> p = parse_sig("48 8B ?? 24");
    assert(p.size() == 4 && p[2] == -1 && p[3] == 0x24);
    unsigned char hay[] = {0x00, 0x48, 0x8B, 0xFF, 0x24, 0x48, 0x8B, 0x00, 0x24, 0x48};
    std::vector<size_t> h = find_sig(hay, sizeof hay, p);
    assert(h.size() == 2 && h[0] == 1 && h[1] == 5);
    assert(find_sig(hay, 3, p).empty());

    puts("SELFTEST OK");
    return 0;
}
#endif
```

- [ ] **Step 3: 실패 확인**

```bash
cd /c/Users/kwon/Desktop/luahook && cmd //c build.bat test
```
기대: 링크 에러 (unresolved external `parse_rules` 등). **컴파일 자체가 되지 않는 것이 이 단계의 정상 결과.**

- [ ] **Step 4: 구현 추가 (선언 바로 아래에)**

```cpp
static std::string unescape(const std::string& in) {
    std::string out;
    for (size_t i = 0; i < in.size(); ++i) {
        if (in[i] == '\\' && i + 1 < in.size()) {
            char n = in[++i];
            out += n == 'n' ? '\n' : n == 't' ? '\t' : n == '\\' ? '\\' : n;
        } else out += in[i];
    }
    return out;
}

Config parse_rules(const std::string& text) {
    Config c;
    std::string t = text;
    if (t.rfind("\xEF\xBB\xBF", 0) == 0) t.erase(0, 3);  // UTF-8 BOM
    size_t pos = 0;
    while (pos <= t.size()) {
        size_t nl = t.find('\n', pos);
        std::string line = t.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? t.size() + 1 : nl + 1;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        size_t p1 = line.find('|');
        if (p1 == std::string::npos) {                       // key=value
            size_t eq = line.find('=');
            if (eq == std::string::npos) continue;
            std::string k = line.substr(0, eq), v = line.substr(eq + 1);
            if (k == "dump") c.dump = v == "1";
            else if (k == "log") c.log = v == "1";
            continue;
        }
        size_t p2 = line.find('|', p1 + 1);
        if (p2 == std::string::npos || line.find('|', p2 + 1) != std::string::npos) continue;  // | 정확히 2개
        c.rules.push_back({line.substr(0, p1), unescape(line.substr(p1 + 1, p2 - p1 - 1)), unescape(line.substr(p2 + 1))});
    }
    return c;
}

size_t replace_all(std::string& s, const std::string& find, const std::string& rep) {
    if (find.empty()) return 0;
    size_t n = 0, pos = 0;
    while ((pos = s.find(find, pos)) != std::string::npos) {
        s.replace(pos, find.size(), rep);
        pos += rep.size();
        ++n;
    }
    return n;
}

std::string sanitize(const char* chunkname) {
    if (!chunkname) return "(null)";
    std::string s = chunkname;
    if (!s.empty() && (s[0] == '@' || s[0] == '=')) s.erase(0, 1);
    for (char& ch : s)
        if (strchr("\\/:*?\"<>|", ch)) ch = '_';
    return s.empty() ? "(empty)" : s;
}

std::vector<int> parse_sig(const char* sig) {
    std::vector<int> out;
    for (const char* p = sig; *p; ) {
        while (*p == ' ') ++p;
        if (!*p) break;
        if (p[0] == '?') out.push_back(-1);
        else out.push_back((int)strtol(std::string(p, 2).c_str(), nullptr, 16));
        p += 2;
    }
    return out;
}

std::vector<size_t> find_sig(const unsigned char* hay, size_t n, const std::vector<int>& pat) {
    std::vector<size_t> hits;
    if (pat.empty() || n < pat.size()) return hits;
    for (size_t i = 0; i + pat.size() <= n; ++i) {
        size_t k = 0;
        for (; k < pat.size(); ++k)
            if (pat[k] != -1 && hay[i + k] != (unsigned char)pat[k]) break;
        if (k == pat.size()) hits.push_back(i);
    }
    return hits;  // ponytail: O(n*m) 단순 스캔. .text 수백KB × 32바이트면 충분히 빠름
}
```

- [ ] **Step 5: 통과 확인**

```bash
cd /c/Users/kwon/Desktop/luahook && cmd //c build.bat test
```
기대: `SELFTEST OK`, 종료코드 0.

- [ ] **Step 6: 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add hook.cpp build.bat && git commit -qm "feat: rules parser, replace, sanitize, sig scan with selftest

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: hook.cpp DLL 부분 (워커 + 후킹 함수 + 로그)

**Files:**
- Modify: `hook.cpp` (Step 2의 `#ifdef SELFTEST ... #endif` 뒤에 `#else` 분기로 추가)
- Create: `rules.txt`

**Interfaces:**
- Consumes: Task 3의 함수 전부, Task 1의 `SIG:` 값, MinHook `MH_Initialize/MH_CreateHook/MH_EnableHook`.
- Produces: `out\hook.dll` (export 없음, DllMain만).

- [ ] **Step 1: rules.txt 작성**

```
# luahook 규칙 파일. hook.dll과 같은 폴더에 둔다. 주입 시 1회 읽음.
# 옵션:  dump=1  → 로드되는 모든 스크립트 원문을 dump\ 에 저장 (처음엔 켜두고 뭘 바꿀지 본다)
#        log=1   → LOAD/DUMP/RESULT 줄 기록 (0이면 REPLACED/ERROR만)
# 규칙:  청크이름포함문자열|찾을문자열|바꿀문자열   (\n \t \\ 이스케이프 지원)
dump=1
log=1
# 예) main.lua|local debug = false|local debug = true
```

- [ ] **Step 2: DLL 코드 추가** — 기존 `#endif` 를 `#else` 로 바꾸고 그 아래에 붙인 뒤 마지막에 `#endif`:

```cpp
#else  // ===================== DLL =====================
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mutex>
#include <fstream>
#include <cstdarg>
#include "MinHook.h"

// Task 1 (tools/find_sig.py) 출력값을 그대로 붙인다.
static const char* LUA_LOAD_SIG = "<<SIG 줄을 여기에>>";

typedef void lua_State;
typedef const char* (*lua_Reader)(lua_State*, void*, size_t*);
typedef int (*lua_load_t)(lua_State*, lua_Reader, void*, const char*, const char*);

static HMODULE      g_self;
static lua_load_t   g_orig;
static Config       g_cfg;
static std::wstring g_base;   // hook.dll 폴더
static std::mutex   g_mu;     // ponytail: 로그·덤프 전역 락 하나
static FILE*        g_log;

static void logf(const char* fmt, ...) {
    if (!g_log) return;
    std::lock_guard<std::mutex> lk(g_mu);
    SYSTEMTIME t; GetLocalTime(&t);
    fprintf(g_log, "[%02d:%02d:%02d.%03d] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list ap; va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
    fputc('\n', g_log); fflush(g_log);
}

static std::wstring widen(const std::string& s) {
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n ? n - 1 : 0, L'\0');
    if (n) MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
    return w;
}

static void dump_chunk(const std::string& chunk, const std::string& fname) {
    std::wstring dir = g_base + L"\\dump";
    CreateDirectoryW(dir.c_str(), nullptr);
    std::ofstream f(dir + L"\\" + widen(fname) + L".lua", std::ios::binary);
    f.write(chunk.data(), (std::streamsize)chunk.size());
    logf("DUMP dump/%s.lua", fname.c_str());
}

// 수집된 청크에 로그/덤프/치환을 적용한다. chunk는 제자리 수정.
static void process(std::string& chunk, const char* name) {
    if (!chunk.empty() && chunk[0] == '\x1b') { logf("BYTECODE skip name=%s", name ? name : "(null)"); return; }
    if (g_cfg.log) logf("LOAD name=%s size=%zu", name ? name : "(null)", chunk.size());
    if (g_cfg.dump) dump_chunk(chunk, sanitize(name));
    std::string nm = name ? name : "";
    for (size_t i = 0; i < g_cfg.rules.size(); ++i) {
        const Rule& r = g_cfg.rules[i];
        if (nm.find(r.name) == std::string::npos) continue;
        size_t n = replace_all(chunk, r.find, r.replace);
        if (n) logf("REPLACED rule=%zu n=%zu", i + 1, n);
        else if (g_cfg.log) logf("RULE_MISS rule=%zu", i + 1);
    }
}

struct Once { const char* p; size_t n; bool done; };
static const char* once_reader(lua_State*, void* ud, size_t* sz) {
    Once* o = (Once*)ud;
    if (o->done) { *sz = 0; return nullptr; }
    o->done = true; *sz = o->n; return o->p;
}

static int hooked_lua_load(lua_State* L, lua_Reader reader, void* data, const char* name, const char* mode) {
    std::string chunk;
    for (;;) {                                   // 원래 reader를 끝까지 소비
        size_t sz = 0;
        const char* p = reader(L, data, &sz);
        if (!p || sz == 0) break;
        chunk.append(p, sz);
    }
    try { process(chunk, name); }
    catch (...) { logf("ERROR exception in process name=%s", name ? name : "(null)"); }
    Once o{ chunk.data(), chunk.size(), false };
    int rc = g_orig(L, once_reader, &o, name, mode);
    if (g_cfg.log || rc) logf("RESULT rc=%d name=%s", rc, name ? name : "(null)");
    return rc;
}

static std::string read_file(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

static DWORD WINAPI worker(LPVOID) {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(g_self, path, MAX_PATH);
    g_base = path; g_base.erase(g_base.find_last_of(L"\\/"));
    g_log = _wfopen((g_base + L"\\hook.log").c_str(), L"a");
    logf("=== attached pid=%lu ===", GetCurrentProcessId());

    g_cfg = parse_rules(read_file(g_base + L"\\rules.txt"));
    logf("rules: %zu, dump=%d, log=%d", g_cfg.rules.size(), g_cfg.dump, g_cfg.log);

    HMODULE x;
    while (!(x = GetModuleHandleW(L"xlua.dll"))) Sleep(100);

    auto* nt = (IMAGE_NT_HEADERS*)((BYTE*)x + ((IMAGE_DOS_HEADER*)x)->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    BYTE* text = nullptr; DWORD tsize = 0;
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++sec)
        if (memcmp(sec->Name, ".text", 5) == 0) { text = (BYTE*)x + sec->VirtualAddress; tsize = sec->Misc.VirtualSize; break; }
    if (!text) { logf("SIG_FAIL no .text"); return 0; }
    logf("xlua.dll found at %p text=%p+0x%lX", x, text, tsize);

    std::vector<size_t> hits = find_sig(text, tsize, parse_sig(LUA_LOAD_SIG));
    if (hits.size() != 1) { logf("SIG_FAIL count=%zu", hits.size()); return 0; }
    void* target = text + hits[0];
    logf("lua_load @ %p (sig match 1)", target);

    MH_STATUS st = MH_Initialize();
    if (st != MH_OK) { logf("MH_Initialize failed %d", st); return 0; }
    st = MH_CreateHook(target, (void*)hooked_lua_load, (void**)&g_orig);
    if (st != MH_OK) { logf("MH_CreateHook failed %d", st); return 0; }
    st = MH_EnableHook(target);
    if (st != MH_OK) { logf("MH_EnableHook failed %d", st); return 0; }
    logf("hook ready");
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = h;
        DisableThreadLibraryCalls(h);
        CreateThread(nullptr, 0, worker, nullptr, 0, nullptr);
    }
    return TRUE;
}
#endif
```

- [ ] **Step 3: `LUA_LOAD_SIG`에 Task 1의 SIG 값 붙이기**

`"<<SIG 줄을 여기에>>"` 를 실제 값(예: `"48 89 5C 24 08 48 89 6C 24 10 ..."`)으로 교체.

- [ ] **Step 4: 셀프테스트가 여전히 통과하는지 + DLL 빌드**

```bash
cd /c/Users/kwon/Desktop/luahook && cmd //c build.bat test && cmd //c build.bat
```
기대: `SELFTEST OK` 후, 두 번째 빌드는 `injector.cpp`가 아직 없어 실패 → **hook.dll까지는 `out\hook.dll`이 생성됐는지 확인**: `ls out/hook.dll`.

- [ ] **Step 5: 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add hook.cpp rules.txt && git commit -qm "feat: lua_load hook DLL with sig scan, dump, replace, log

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: injector.cpp

**Files:**
- Create: `injector.cpp`

**Interfaces:**
- Consumes: `out\hook.dll` (같은 폴더).
- Produces: `out\injector.exe [exe이름=msw.exe] [dll경로]`.

- [ ] **Step 1: 작성**

```cpp
// injector.cpp — 대상 exe가 뜨길 기다렸다가 hook.dll을 LoadLibraryW 원격 스레드로 주입한다.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <string>

static DWORD find_pid(const wchar_t* name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    PROCESSENTRY32W e{ sizeof e };
    DWORD pid = 0;
    if (Process32FirstW(snap, &e))
        do { if (_wcsicmp(e.szExeFile, name) == 0) { pid = e.th32ProcessID; break; } } while (Process32NextW(snap, &e));
    CloseHandle(snap);
    return pid;
}

static int fail(const wchar_t* msg) {
    wprintf(L"%s (오류 %lu)\n아무 키나 누르면 닫힙니다.", msg, GetLastError());
    getchar();
    return 1;
}

int wmain(int argc, wchar_t** argv) {
    const wchar_t* exe = argc > 1 ? argv[1] : L"msw.exe";
    std::wstring dll;
    if (argc > 2) dll = argv[2];
    else {
        wchar_t p[MAX_PATH]; GetModuleFileNameW(nullptr, p, MAX_PATH);
        dll = p; dll.erase(dll.find_last_of(L"\\/") + 1); dll += L"hook.dll";
    }
    if (GetFileAttributesW(dll.c_str()) == INVALID_FILE_ATTRIBUTES) return fail(L"hook.dll을 찾을 수 없습니다");

    wprintf(L"%s 대기 중... (게임을 실행하세요)\n", exe);
    DWORD pid;
    while (!(pid = find_pid(exe))) Sleep(500);
    wprintf(L"pid %lu 발견. 2초 후 주입합니다.\n", pid);
    Sleep(2000);

    HANDLE h = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!h) return fail(L"OpenProcess 실패. 관리자 권한으로 실행하세요");

    SIZE_T bytes = (dll.size() + 1) * sizeof(wchar_t);
    void* mem = VirtualAllocEx(h, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!mem || !WriteProcessMemory(h, mem, dll.c_str(), bytes, nullptr)) return fail(L"메모리 쓰기 실패");

    auto loadlib = (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HANDLE t = CreateRemoteThread(h, nullptr, 0, loadlib, mem, 0, nullptr);
    if (!t) return fail(L"CreateRemoteThread 실패");
    WaitForSingleObject(t, INFINITE);
    DWORD rc = 0; GetExitCodeThread(t, &rc);
    VirtualFreeEx(h, mem, 0, MEM_RELEASE);
    CloseHandle(t); CloseHandle(h);

    if (!rc) return fail(L"대상 안에서 LoadLibrary 실패 (hook.dll 비트/의존성 확인)");
    wprintf(L"주입 완료. hook.log를 확인하세요.\n아무 키나 누르면 닫힙니다.");
    getchar();
    return 0;
}
```

- [ ] **Step 2: 전체 빌드**

```bash
cd /c/Users/kwon/Desktop/luahook && cmd //c build.bat && ls out
```
기대: `BUILD OK`, `out/` 에 `hook.dll injector.exe rules.txt`.

- [ ] **Step 3: 인젝터 단독 동작 확인 (게임 없이)**

```bash
cd /c/Users/kwon/Desktop/luahook && (timeout 3 ./out/injector.exe nonexist.exe; true)
```
기대: `nonexist.exe 대기 중...` 출력 후 3초에 timeout으로 끊김 (폴링 루프 정상).

- [ ] **Step 4: 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add injector.cpp && git commit -qm "feat: injector

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 게임 통합 테스트 (사용자 참여)

**Files:** 없음 (검증만). 결과에 따라 `rules.txt` 예시 갱신.

- [ ] **Step 1: 사전 조건 확인** — 게임이 꺼져 있어야 함. 켜져 있으면 사용자에게 종료 요청.

- [ ] **Step 2: 사용자에게 안내**

> 1. `C:\Users\kwon\Desktop\luahook\out\injector.exe`를 **마우스 오른쪽 → 관리자 권한으로 실행**
> 2. 콘솔에 "msw.exe 대기 중..."이 뜨면 게임 실행
> 3. "주입 완료"가 뜨면 알려주세요

- [ ] **Step 3: 로그 확인**

```bash
cat /c/Users/kwon/Desktop/luahook/out/hook.log; ls /c/Users/kwon/Desktop/luahook/out/dump | head -30; ls /c/Users/kwon/Desktop/luahook/out/dump | wc -l
```
판정:
| 로그 | 의미 | 조치 |
|---|---|---|
| `hook ready` 없음, `SIG_FAIL count=0` | 런타임 .text가 파일과 다름 (재배치는 무관하나 패킹/암호화 가능성) | 사용자에게 보고. `NexonGlobalSecurity64.dll` 관련 가능성 언급 |
| `SIG_FAIL count>1` | 시그니처가 짧음 | find_sig.py `SIG_LEN` 48로 올려 재추출 |
| `MH_* failed` | 후킹 실패 | 상태 코드로 MinHook 문서 확인 후 보고 |
| `hook ready` 있고 `LOAD` 없음 | 주입이 너무 늦었거나 lua_load를 안 지남 | 게임 재시작 후 injector 먼저 실행 재시도. 그래도 없으면 보고 |
| `LOAD` 다수 + `dump/` 파일들 | **성공** | Step 4 |

- [ ] **Step 4: 실제 치환 1건 검증**

dump 중 짧은 스크립트 하나를 골라 눈에 띄는 리터럴(예: 문자열 상수)을 찾아 `out\rules.txt`에 규칙 1줄 추가 → 게임 종료 → injector → 게임 실행 → 로그에 `REPLACED rule=1 n=1` + 이어지는 `RESULT rc=0` 확인. `rc≠0`이면 치환이 문법을 깬 것이므로 규칙 수정.

- [ ] **Step 5: 확인된 규칙을 저장소 `rules.txt`의 주석 예시로 반영 후 커밋**

```bash
cd /c/Users/kwon/Desktop/luahook && git add rules.txt && git commit -qm "docs: working rule example from integration test

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 자체 검토

- **스펙 커버리지**: §4.1 인젝터→Task 5, §4.2 워커/후킹/process→Task 4, §4.3 rules→Task 3+4, §4.4 로그 형식→Task 4 `logf` 호출들, §5 스파이크→Task 1, §6 빌드→Task 3 Step 1, §7 테스트→Task 3(단위)/Task 1(시그니처)/Task 6(통합). §8 "하지 않는 것"은 코드에 없음이 곧 구현. 누락 없음.
- **타입 일관성**: `Config/Rule/parse_rules/replace_all/sanitize/parse_sig/find_sig` 시그니처가 Task 3 선언, Task 3 구현, Task 4 사용에서 동일. `lua_Reader`/`Once`/`once_reader`는 Task 4 안에서만 쓰임.
- **알려진 미확정**: Task 1의 사슬 가정(호출자 1개)이 깨질 수 있음 — 그 경우 중단·보고로 명시. `NexonGlobalSecurity64.dll` 존재는 조사 중 발견 — 런타임 텍스트 무결성 검사를 한다면 Task 6에서 드러남.
