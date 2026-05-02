#SingleInstance Force
#NoEnv

#Include %A_ScriptDir%\ft_lib.ahk

; FindText 클래스가 로드되었는지 테스트
ft := new FindText()
FileAppend, FindText loaded successfully, %A_Temp%\test_include.txt
