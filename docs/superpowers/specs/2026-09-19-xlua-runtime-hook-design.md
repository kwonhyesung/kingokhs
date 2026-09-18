# xLua 런타임 스크립트 교체 DLL — 설계

날짜: 2026-09-19
목적: 학습용. 안티치트 없는 64비트 Unity(xLua) 게임에서, 특정 이름의 Lua 스크립트가
로드되는 순간 평문 내용을 교체하고 그 과정을 로그로 남긴다.

## 1. 확정된 전제

| 항목 | 값 |
|---|---|
| 대상 | 64비트 게임 exe, `xlua.dll` 별도 로드 |
| Lua 형태 | 평문 텍스트 (바이트코드 아님) |
| 교체 대상 | 로드되는 Lua 청크 내용(buff) 안의 문자열 |
| 주입 방식 | 별도 인젝터 exe (`CreateRemoteThread` + `LoadLibraryW`) |
| 안티치트 | 없음 |
| 빌드 도구 | VS 2022 Build Tools, MSVC 14.34 (`vcvars64.bat` + `cl.exe`) |

## 2. 전체 흐름

```
[injector.exe]                    [게임 프로세스]
  exe 이름 폴링(500ms)
  발견 → OpenProcess
  VirtualAllocEx(dll 경로)
  CreateRemoteThread(LoadLibraryW) ───▶ hook.dll 로드
  종료                                 DllMain → 워커 스레드
                                       xlua.dll 뜰 때까지 폴링(100ms)
                                       luaL_loadbufferx 후킹(MinHook)
                                       ...
                                       게임이 스크립트 로드
                                         → 후킹 함수
                                           ① 로그 기록
                                           ② (옵션) 원문 덤프
                                           ③ 규칙 매칭 → 문자열 치환
                                           ④ 원본 함수 호출(치환된 버퍼)
```

## 3. 파일 구성

```
luahook/
  injector.cpp      인젝터. 약 60줄
  hook.cpp          DLL 본체. 약 150줄
  rules.txt         교체 규칙 + 옵션. 재빌드 없이 수정
  build.bat         vcvars64 호출 + cl 두 번
  minhook/          MinHook 원본 그대로 (MIT). buffer.c hook.c trampoline.c hde64.c + 헤더
  README.md         사용법 5줄
```

빌드 산출물: `luahook/out/injector.exe`, `luahook/out/hook.dll`. `rules.txt`, `hook.log`, `dump/`는
**hook.dll과 같은 폴더** 기준으로 읽고 쓴다 (`GetModuleFileNameW(자기 자신)`로 경로 구함. 게임 작업 디렉터리에
의존하지 않기 위함).

## 4. 컴포넌트 상세

### 4.1 injector.exe

- 사용법: `injector.exe <게임exe이름> [hook.dll 경로]` — dll 경로 생략 시 injector 옆의 `hook.dll`.
- 동작:
  1. `CreateToolhelp32Snapshot` + `Process32NextW`로 이름이 일치하는 PID 검색. 없으면 500ms 대기 후 반복.
  2. 발견 시 `OpenProcess(PROCESS_ALL_ACCESS)`. 실패(권한)면 메시지 출력 후 종료 — "관리자 권한으로 실행" 안내.
  3. `VirtualAllocEx` → `WriteProcessMemory`(dll 절대경로, wide) → `CreateRemoteThread(LoadLibraryW)`.
  4. `WaitForSingleObject` 후 `GetExitCodeThread` 값(=HMODULE 하위 32비트)이 0이면 "로드 실패" 출력. (전체 핸들은 못 얻지만 0/비0 판별엔 충분.)
  5. 종료.
- 실행 후 게임 exe가 다시 뜨면 재주입하려면 injector를 다시 실행한다. (자동 재주입 루프는 안 함.)

### 4.2 hook.dll

**진입**
- `DllMain(DLL_PROCESS_ATTACH)`: `DisableThreadLibraryCalls`, `CreateThread(Worker)`. DllMain 안에서는 아무것도 안 함(로더 락).

**Worker 스레드**
1. 자기 경로 → 기준 폴더 결정, `hook.log` 열기(append), `"=== attached pid=... ==="` 기록.
2. `rules.txt` 파싱 (4.3).
3. `GetModuleHandleW(L"xlua.dll")`이 NULL이면 100ms sleep 반복. 최대 대기 없음(게임이 끝나면 프로세스와 함께 죽음).
4. `GetProcAddress(xlua, "luaL_loadbufferx")`. NULL이면 `"xluaL_loadbuffer"`로 대체 시도. 둘 다 없으면 export 목록을 로그에 남기고 종료. (xLua 빌드에 따라 lua5.3/luajit 어느 쪽이든 `luaL_loadbufferx`는 있는 게 정상.)
5. `MH_Initialize` → `MH_CreateHook(원본, Hooked, &orig)` → `MH_EnableHook`. 각 단계 결과를 로그에.
6. `"hook ready"` 기록 후 스레드 종료.

**후킹 함수** — 시그니처(Lua 5.3/LuaJIT 공통):
```c
int luaL_loadbufferx(lua_State* L, const char* buff, size_t sz, const char* name, const char* mode)
```
`xluaL_loadbuffer`로 대체된 경우 `(L, buff, int size, name)` 4인자. 두 시그니처는 별도 함수로 두고 공통 로직 `process(buff, sz, name) -> std::string*`만 공유.

`process` 로직:
1. 로그: `LOAD name=<name> size=<sz>`. name이 NULL이면 `"(null)"`.
2. `dump=1`이면 `dump/<sanitized name>.lua`에 원문 저장. sanitize: `\ / : * ? " < > |`를 `_`로, 선두 `@`/`=` 제거. 같은 이름 재로드 시 덮어씀.
3. 규칙 순회: `name`에 `rule.name`이 **부분 문자열로 포함**되면 후보. 후보 규칙마다 `find`를 **전부** `replace`로 치환(std::string::find 반복). 치환 횟수 0이면 `RULE_MISS`, 1 이상이면 `REPLACED n=<횟수>` 로그.
4. 하나라도 치환됐으면 새 `std::string`을 반환하고 호출자는 그 `data()/size()`로 원본 함수 호출. 아니면 원래 인자 그대로.
5. 버퍼 수명: 원본 `luaL_loadbufferx`는 호출 중에만 buff를 읽고 복사하므로(파서가 즉시 소비) 스택의 `std::string`이면 충분.

**스레드 안전**: Lua 로드는 보통 메인 스레드지만 보장은 없으므로 로그 파일 쓰기는 `std::mutex` 하나로 감싼다.
(`ponytail: 전역 락 하나. 성능 문제 될 일 없음.`)

**예외**: 후킹 함수 안에서 C++ 예외가 게임으로 새면 크래시. `process` 전체를 `try/catch(...)`로 감싸고 실패 시 원본 인자 그대로 통과 + `ERROR` 로그.

### 4.3 rules.txt 형식

```
# 주석. 빈 줄 무시.
dump=1
log=1
main.lua|local debug = false|local debug = true
main.lua|return 10|return 999
ui/shop|price =|price = 0 *
```

- `key=value` 줄은 옵션. 현재 옵션: `dump` (0/1, 기본 0), `log` (0/1, 기본 1).
- `name|find|replace` 줄이 규칙. `|`는 정확히 2개. `find`/`replace`에 `|`가 필요하면 지원 안 함(YAGNI).
- 이스케이프: `\n`, `\t`, `\` 세 가지만 처리. 여러 줄 치환에 필요.
- 파일 인코딩: UTF-8, BOM 있으면 제거.
- 파싱 시점: 주입 시 1회. 바꾸면 게임 재시작+재주입. (핫리로드 안 함.)

### 4.4 hook.log 형식

```
[12:34:56.789] === attached pid=1234 base=C:\...\luahook\out ===
[12:34:56.790] rules: 3, dump=1
[12:34:58.011] xlua.dll found at 00007FF8...
[12:34:58.012] hook target luaL_loadbufferx @ 00007FF8...
[12:34:58.013] hook ready
[12:35:01.500] LOAD name=@main.lua size=4821
[12:35:01.501] DUMP dump/main.lua
[12:35:01.501] REPLACED rule=1 n=1
[12:35:01.502] RULE_MISS rule=2
```

## 5. 빌드

`build.bat`:
```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cl /O2 /EHsc /std:c++17 /LD hook.cpp minhook\*.c /Fe:out\hook.dll user32.lib
cl /O2 /EHsc /std:c++17 injector.cpp /Fe:out\injector.exe
```
(정확한 vcvars 경로는 구현 시 확인.)

MinHook 소스는 GitHub `TsudaKageyu/minhook` `src/` + `include/` 그대로 복사. 버전 고정 표기.

## 6. 테스트

- **단위(게임 없이)**: `hook.cpp`를 `/DSELFTEST`로 exe 빌드하면 `main()`이 실행되어
  - rules 파서: 옵션/규칙/이스케이프/주석 처리 assert
  - 치환 함수: 0회/다회/길이 변화 assert
  - sanitize: 금지 문자 치환 assert
  `build.bat test`로 실행.
- **통합(게임)**: injector 실행 → 게임 실행 → `hook.log`에 `hook ready`와 `LOAD` 줄이 찍히는지 → `dump/`에 스크립트가 생기는지 → 덤프 보고 규칙 작성 → 재주입 후 `REPLACED` 확인 → 게임 동작 변화 확인.

## 7. 하지 않는 것 (필요해지면 그때)

- 바이트코드(`\x1bLua`) 청크 처리 — 감지되면 `BYTECODE skip` 로그만 남김
- 정규식 치환, 여러 `|` 이스케이프
- rules.txt 핫리로드
- 인젝터 자동 재주입 루프 / GUI
- `lua_load`, `luaL_loadfilex` 등 다른 진입점 후킹 — xLua는 파일 로드도 C#에서 읽어 `loadbuffer`로 넘기므로 불필요. 로그에 안 찍히는 스크립트가 생기면 그때 추가.
- 언훅/DLL 언로드 — 게임 종료와 함께 사라짐

## 8. 위험 요소

| 위험 | 대응 |
|---|---|
| xLua 빌드가 `luaL_loadbufferx`를 export 안 함 | `xluaL_loadbuffer` fallback + export 목록 로그 |
| il2cpp가 P/Invoke 주소를 캐시해도 인라인 후킹이라 무관 | — |
| 주입 타이밍이 xlua 로드 이전 | 워커가 폴링하므로 문제 없음 |
| 인젝터 권한 부족 | 관리자 실행 안내 메시지 |
| 치환 결과가 문법 오류 | Lua 쪽에서 로드 실패 → 게임이 에러 처리. 로그의 원본 반환값(0이 아니면 실패)도 기록해 확인 가능 |
