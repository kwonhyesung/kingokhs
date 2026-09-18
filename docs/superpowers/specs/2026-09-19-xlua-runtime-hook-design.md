# xLua 런타임 스크립트 교체 DLL — 설계

날짜: 2026-09-19
목적: 학습용. MapleStory Worlds(Unity + xLua, 64비트, 안티치트 없음)에서 특정 이름의 Lua
스크립트가 로드되는 순간 평문 내용을 교체하고 그 과정을 로그로 남긴다.

## 1. 확정된 전제

| 항목 | 값 |
|---|---|
| 대상 exe | `C:\Nexon\MapleStory Worlds\msw.exe` (64비트) |
| xlua.dll | `msw_Data\Plugins\x86_64\xlua.dll`, Lua 5.3 기반 |
| xlua.dll export | **487개 전부 `_mlNNN`으로 난독화** (`check_wrapper_version` 하나만 예외). `GetProcAddress`로 Lua 함수를 못 찾음 → **런타임 시그니처 스캔** |
| 후킹 대상 | `luaL_loadbufferx(L, buff, sz, name, mode)` — 게임은 C#에서 `xluaL_loadbuffer`로 바이트를 넘기므로 이 함수를 지난다. (`lua_load`는 이 빌드에서 호출자에 인라인돼 후킹 불가. 전체 커버가 필요하면 `luaD_protectedparser`, 10절 ALT) |
| Lua 형태 | 평문 텍스트 (바이트코드 아님) |
| 교체 대상 | 로드되는 Lua 청크 내용 안의 문자열 |
| 주입 방식 | 별도 인젝터 exe (`CreateRemoteThread` + `LoadLibraryW`), 발견 후 2초 대기 |
| 안티치트 | 없음. 단 넥슨 약관상 클라이언트 개조는 금지 → 본인 학습/본인 월드 범위에서만 |
| 프로젝트 위치 | `C:\Users\kwon\Desktop\luahook\` 별도 git 저장소 |
| 빌드 도구 | VS 2022 Build Tools, MSVC 14.34 + Windows SDK 10.0.20348 (`vcvars64.bat` + `cl.exe`). IDE 불필요 |

## 2. 전체 흐름

```
[injector.exe]                    [msw.exe]
  msw.exe 폴링(500ms)
  발견 → 2초 대기 → OpenProcess
  VirtualAllocEx(dll 경로)
  CreateRemoteThread(LoadLibraryW) ───▶ hook.dll 로드
  종료                                 DllMain → 워커 스레드
                                       xlua.dll 뜰 때까지 폴링(100ms)
                                       .text 시그니처 스캔 → luaL_loadbufferx 주소
                                       luaL_loadbufferx 후킹(MinHook)
                                       ...
                                       게임이 스크립트 로드 → luaL_loadbufferx
                                         → 후킹 함수
                                           ① buff[0..sz)를 std::string으로 복사 (버퍼가 이미 주어짐)
                                           ② 로그 기록
                                           ③ (옵션) 원문 덤프
                                           ④ 규칙 매칭 → 문자열 치환
                                           ⑤ 치환된 문자열의 data/size로 원본 luaL_loadbufferx 호출
                                           ⑥ 반환값 로그
```

## 3. 파일 구성

```
luahook/
  injector.cpp        인젝터. 약 60줄
  hook.cpp            DLL 본체. 약 200줄 (시그니처 스캔 포함)
  rules.txt           교체 규칙 + 옵션. 재빌드 없이 수정
  build.bat           vcvars64 호출 + cl 두 번 (+ `test` 인자로 셀프테스트)
  minhook/            MinHook v1.3.4 원본 그대로 (MIT). src/*.c + include/MinHook.h
  tools/find_sig.py   시그니처 추출 스크립트 (pefile + capstone). 게임 업데이트 시 재실행
  README.md           사용법 5줄
  out/                빌드 산출물: injector.exe, hook.dll (+ rules.txt 복사, hook.log, dump/)
```

`rules.txt`, `hook.log`, `dump/`는 **hook.dll과 같은 폴더** 기준으로 읽고 쓴다
(`GetModuleFileNameW(자기 자신)`으로 경로 구함. 게임 작업 디렉터리에 의존하지 않기 위함).

## 4. 컴포넌트 상세

### 4.1 injector.exe

- 사용법: `injector.exe [게임exe이름] [hook.dll 경로]` — 기본 `msw.exe`, dll 경로 생략 시 injector 옆의 `hook.dll`.
- 동작:
  1. `CreateToolhelp32Snapshot` + `Process32NextW`로 이름이 일치하는 PID 검색. 없으면 500ms 대기 후 반복.
  2. 발견 시 2초 대기(Unity 초기화 여유) 후 `OpenProcess(PROCESS_ALL_ACCESS)`. 실패(권한)면 "관리자 권한으로 실행" 안내 후 종료.
  3. `VirtualAllocEx` → `WriteProcessMemory`(dll 절대경로, wide) → `CreateRemoteThread(LoadLibraryW)`.
  4. `WaitForSingleObject` 후 `GetExitCodeThread` 값(=HMODULE 하위 32비트)이 0이면 "로드 실패" 출력.
  5. 종료.
- 게임을 다시 켰으면 injector도 다시 실행. (자동 재주입 루프는 안 함.)
- 사용 순서: **injector 먼저 → 게임 실행**. 게임이 이미 떠 있으면 이미 로드된 스크립트는 못 잡고 이후 로드만 보임.

### 4.2 hook.dll

**진입**
- `DllMain(DLL_PROCESS_ATTACH)`: `DisableThreadLibraryCalls`, `CreateThread(Worker)`. DllMain 안에서는 그 외 아무것도 안 함(로더 락).

**Worker 스레드**
1. 자기 경로 → 기준 폴더 결정, `hook.log` 열기(append), `=== attached pid=... ===` 기록.
2. `rules.txt` 파싱 (4.3).
3. `GetModuleHandleW(L"xlua.dll")`이 NULL이면 100ms sleep 반복. 최대 대기 없음.
4. xlua.dll의 `.text` 섹션에서 `LOADBUFFERX_SIG`(10절, `??` 와일드카드 허용)를 스캔. 매치가 정확히 1개가 아니면 `SIG_FAIL count=N` 로그 후 종료 (게임은 정상 동작).
5. `MH_Initialize` → `MH_CreateHook(원본, Hooked_loadbufferx, &orig)` → `MH_EnableHook`. 각 단계 결과 로그.
6. `hook ready` 기록 후 스레드 종료.

**후킹 함수** — Lua 5.3 시그니처:
```c
int luaL_loadbufferx(lua_State* L, const char* buff, size_t sz, const char* name, const char* mode);
```
동작:
1. `std::string chunk(buff, sz)` 로 복사.
2. `process(chunk, name)` → 최종 문자열.
3. `orig(L, chunk.data(), chunk.size(), name, mode)` 호출.
4. 반환값(0=성공, 그 외 문법 오류 등)을 `RESULT rc=N` 로그. 에러 메시지 문자열은 `lua_tolstring`도 난독화라 생략 (`ponytail: 필요해지면 lua_tolstring 시그니처도 뽑기`).

`process` 로직:
1. 첫 바이트가 `\x1b`(바이트코드)면 `BYTECODE skip` 로그 후 원문 그대로 반환.
2. 로그: `LOAD name=<name> size=<sz>`. name NULL이면 `(null)`.
3. `dump=1`이면 `dump/<sanitized name>.lua`에 원문 저장. sanitize: `\ / : * ? " < > |` → `_`, 선두 `@`/`=` 제거. 같은 이름은 덮어씀.
4. 규칙 순회: `name`에 `rule.name`이 **부분 문자열로 포함**되면 후보. 후보 규칙마다 `find`를 **전부** `replace`로 치환. 0회면 `RULE_MISS rule=i`, 1회 이상이면 `REPLACED rule=i n=<횟수>` 로그.

**스레드 안전**: 로그 파일 쓰기는 `std::mutex` 하나로 감싼다. (`ponytail: 전역 락 하나. 성능 문제 될 일 없음.`)

**예외**: 후킹 함수 전체를 `try/catch(...)`로 감싼다. 실패 시 `ERROR` 로그 + 원래 인자 그대로 원본에 전달.

### 4.3 rules.txt 형식

```
# 주석. 빈 줄 무시.
dump=1
log=1
main.lua|local debug = false|local debug = true
main.lua|return 10|return 999
ui/shop|price =|price = 0 *
```

- `key=value` 줄은 옵션. `dump` (0/1, 기본 **1**), `log` (0/1, 기본 1).
- `name|find|replace` 줄이 규칙. `|`는 정확히 2개. `find`/`replace`에 `|`가 들어가는 경우는 지원 안 함.
- 이스케이프: `\n`, `\t`, `\\` 세 가지만. 여러 줄 치환용.
- UTF-8, BOM 있으면 제거.
- 주입 시 1회 파싱. 바꾸면 게임 재시작 + 재주입.

### 4.4 hook.log 형식

```
[12:34:56.789] === attached pid=1234 base=C:\Users\kwon\Desktop\luahook\out ===
[12:34:56.790] rules: 3, dump=1
[12:34:58.011] xlua.dll found at 00007FF8... text=00007FF8...+0x9A000
[12:34:58.012] luaL_loadbufferx @ 00007FF8... (sig match 1)
[12:34:58.013] hook ready
[12:35:01.500] LOAD name=@main.lua size=4821
[12:35:01.501] DUMP dump/main.lua
[12:35:01.501] REPLACED rule=1 n=1
[12:35:01.502] RULE_MISS rule=2
[12:35:01.503] RESULT rc=0
```

## 5. 시그니처 확보 (스파이크, 구현 전 1회)

- `tools/find_sig.py`: `pefile` + `capstone`으로 xlua.dll 정적 분석. 게임 파일은 읽기만 함.
- 추적 사슬 (Lua 5.3 소스 구조 기준):
  1. `.rdata`에서 문자열 `"attempt to load a %s chunk (mode is '%s')"` 주소 찾기
  2. `.text`에서 그 주소를 `lea reg,[rip+disp]`로 참조하는 함수 = `f_parser` (`checkmode` 인라인). 함수 시작은 직전 `int3`/`nop` 패딩 경계로 판단
  3. `f_parser` 시작 주소를 `lea`로 넘기는 함수 = `luaD_protectedparser`
  4. `luaD_protectedparser`를 `call`하는 함수 중 `luaL_loadbufferx` (lua_load 인라인, export 4개가 호출). 판별: `'?'` 문자열 참조 + export 호출자 존재
- `luaL_loadbufferx` 시작부터 24~40바이트를 뽑고, RIP 상대 주소·절대 주소 바이트는 `??`로. 파일 전체에서 **정확히 1회** 매치되는지 검증.
- 산출물: 시그니처 + 근거(각 단계 RVA, 디스어셈블 몇 줄)를 10절에 기록. 스크립트는 `tools/`에 보관 (게임 업데이트 시 재실행).
- 실패 조건: 사슬이 끊기거나 후보가 여럿이면 중단 후 보고. 대안: x64dbg로 수동 확인.

## 6. 빌드

`build.bat`:
```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cl /O2 /EHsc /std:c++17 /LD hook.cpp minhook\src\*.c /I minhook\include /Fe:out\hook.dll
cl /O2 /EHsc /std:c++17 injector.cpp /Fe:out\injector.exe
copy /Y rules.txt out\
```
`build.bat test` → `cl /DSELFTEST /EHsc /std:c++17 hook.cpp /Fe:out\selftest.exe && out\selftest.exe`.
(vcvars 경로는 구현 시 재확인.)

## 7. 테스트

- **시그니처 검증(게임 없이)**: `python tools/find_sig.py` 재실행 → 패턴이 xlua.dll에서 정확히 1회 매치되는지 assert.
- **단위(게임 없이)**: `build.bat test` — `hook.cpp`의 `main()`이
  - rules 파서: 옵션/규칙/이스케이프/주석/BOM assert
  - 치환: 0회/다회/길이 변화 assert
  - sanitize: 금지 문자 치환, 선두 `@` 제거 assert
  - 시그니처 스캐너: `??` 포함 패턴을 테스트 바이트열에서 찍어보기 assert
- **통합(게임)**: injector 실행 → 게임 실행 → `hook.log`에 `hook ready`·`LOAD` 확인 → `dump/`에 스크립트 생성 확인 → 덤프 보고 규칙 작성 → 재시작+재주입 → `REPLACED` + `RESULT rc=0` 확인 → 게임 동작 변화 확인.

## 8. 하지 않는 것 (필요해지면 그때)

| 항목 | 이유 |
|---|---|
| 바이트코드 청크 처리 | 평문 확인됨. 바이트코드면 디컴파일이 필요한 별개 프로젝트. `BYTECODE skip` 로그만 |
| 정규식 치환, `|` 이스케이프 | 학습엔 문자열 치환으로 충분. 필요 시 20줄 |
| rules.txt 핫리로드 | 게임 재시작으로 대체 |
| 인젝터 자동 재주입 / GUI | 더블클릭으로 대체 |
| Lua 에러 메시지 문자열 | `lua_tolstring` 시그니처까지 필요. 반환 코드로 우선 판별 |
| 언훅 / DLL 언로드 | 게임 종료와 함께 사라짐 |
| `luaL_loadfilex` / `load(function)` 경로 | 게임은 C#에서 바이트를 `xluaL_loadbuffer`로 넘김. 로그에 안 찍히는 스크립트가 있으면 ALT_SIG(`luaD_protectedparser`)로 전환 |

## 9. 위험 요소

| 위험 | 대응 |
|---|---|
| 게임 업데이트로 xlua.dll 변경 → 시그니처 불일치 | `SIG_FAIL` 로그, 게임은 정상. `tools/find_sig.py` 재실행해 패턴 갱신 |
| 시그니처가 2곳 이상 매치 | 스파이크에서 1회 매치 검증. 런타임도 1개 아니면 후킹 안 함 |
| il2cpp가 P/Invoke 주소를 캐시 | 인라인 후킹이라 무관 |
| 주입 타이밍이 xlua 로드 이전 | 워커가 폴링 |
| 인젝터 권한 부족 | 관리자 실행 안내 |
| 치환 결과가 문법 오류 | `RESULT rc≠0` 로그로 확인 |
| 약관 | 본인 학습/본인 월드 범위에서만 사용 |

## 10. 시그니처 (스파이크 결과)

- xlua.dll SHA-256 앞 16자: `2663440a12f5f474` (1,445,856 bytes, 2026-08-18)
- string RVA `0x128588` → f_parser `0x135a0` (checkmode 인라인, 오류 블록은 `0x13773` 콜드 조각) → luaD_protectedparser `0x137d0` → 호출자 4개 중 **luaL_loadbufferx `0x63d0`**
- `lua_load`는 이 빌드에서 호출자 대부분에 **인라인**돼 있어 단일 후킹 지점이 아니다. `luaD_protectedparser` 호출자:
  | RVA | 정체 | 비고 |
  |---|---|---|
  | `0x63d0` | **`luaL_loadbufferx`** (lua_load 인라인, `getS` 리더 `0x63b0`) | export 4개(`_ml482 _ml94 _ml390 _ml173`) 포함 호출자 8개 — 게임 스크립트 주 경로. **후킹 대상** |
  | `0x2dd0` | 독립 `lua_load` | 호출자 `luaB_load`(`0x8950`, `load(function)` 경로) 하나뿐. export 아님 |
  | `0x5ed0` | `luaL_loadfilex` (lua_load 인라인, `"=stdin"`) | 놓치는 경로 (8절) |
  | `0xf100` | `db_debug` (lua_load 인라인, `"lua_debug> "`) | 놓치는 경로 |
- 후킹 함수 (인자 확인: rcx=L, rdx=buff, r8=sz, r9=name→NULL이면 `"?"`, `[rsp+0x90]`=mode):
  ```c
  int luaL_loadbufferx(lua_State* L, const char* buff, size_t sz, const char* name, const char* mode);
  ```
- **Primary** `LOADBUFFERX_SIG = "4C 8B DC 53 48 83 EC 60 4D 89 43 C0 48 8D 05 ?? ?? ?? ?? 49 89 43 D8 4C 8D 05 ?? ?? ?? ?? 49 8D 43 B8"` (luaL_loadbufferx `0x63d0`, 파일 전체 1회 매치)
- **ALT** (전체 경로 커버용, 향후) `PROTECTEDPARSER_SIG = "48 8B C4 48 89 58 08 48 89 68 10 48 89 70 18 57 41 54 41 55 41 56 41 57 48 81 EC ?? ?? ?? ?? 0F B7 A9 C4 00 00 00"` (luaD_protectedparser `0x137d0`, 1회 매치). 시그니처: `int luaD_protectedparser(lua_State* L, ZIO* z, const char* name, const char* mode)`, `struct ZIO { size_t n; const char* p; lua_Reader reader; void* data; lua_State* L; }` (0x28 bytes, `0x63d0` 디스어셈블로 확인). 이걸 쓰면 `z->p[0..n)` + `z->reader` 루프로 원문을 모으고 `z->n/p/reader`를 우리 버퍼로 바꿔 원본 호출.
- 추출: `python tools/find_sig.py` (2026-09-19). 스크립트 조정: `.pdata`의 `UNW_FLAG_CHAININFO` 조각을 원 함수로 귀속, 어디서도 참조되지 않는 죽은 `checkmode` 복사본(`0x13530`) 제외, 4단계를 "호출자 중 `'?'` 참조 + export 호출자 있는 함수 = luaL_loadbufferx"로 판별.
