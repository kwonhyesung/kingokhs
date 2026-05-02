#SingleInstance Force
#NoEnv
SetBatchLines, -1

; ft.ahk FindText 함수 호출을 위한 래퍼 스크립트 (AHK v1)
; 사용법: findtext_ahk.ahk [pattern] [x1] [y1] [x2] [y2]
; 결과: 임시 파일에 JSON 형식의 결과 출력

#Include %A_ScriptDir%\ft_lib.ahk

if (0 = %0%)
{
    FileAppend, Usage: findtext_ahk.ahk pattern x1 y1 x2 y2`n, *
    ExitApp, 1
}

pattern := A_Args[1]
x1 := A_Args[2]
y1 := A_Args[3]
x2 := A_Args[4]
y2 := A_Args[5]

if (x1 = "")
    x1 := 0
if (y1 = "")
    y1 := 0
if (x2 = "")
    x2 := A_ScreenWidth
if (y2 = "")
    y2 := A_ScreenHeight

; FindText 인스턴스 생성
ft := new FindText()

; 패턴 검색
result := ft.FindText(pattern, x1, y1, x2, y2, 0, 0, 0, 0, 0)

; 결과를 JSON 형식으로 변환
json := "["
for index, obj in result
{
    if (index > 1)
        json .= ","
    json .= "{""x"":" obj.x ",""y"":" obj.y ",""id"":""" obj.id """}"
}
json .= "]"

; 결과를 임시 파일에 쓰기
output_file := A_Temp "\findtext_result.txt"
FileDelete, %output_file%
FileAppend, %json%, %output_file%

