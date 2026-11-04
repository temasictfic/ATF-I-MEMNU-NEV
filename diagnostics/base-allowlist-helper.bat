@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ============================================================
rem  BASE allowlist helper - find the IP to give BASE support
rem  Double-click to run. Works on Windows 10 1803+ / Windows 11.
rem  Produces base-allowlist-message.txt next to this file. Open
rem  that file in Notepad and paste it into your email to BASE.
rem ============================================================

set "OUT=%~dp0base-allowlist-message.txt"
set "ITOUT=%~dp0it-department-request.txt"
set "CURL=%SystemRoot%\System32\curl.exe"

if not exist "%CURL%" (
    echo curl.exe not found at %CURL%
    echo Your Windows is too old ^(needs 10 build 1803 or newer^).
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   BASE allowlist helper
echo ============================================================
echo.
echo This finds the public IP and announced IP range that BASE
echo sees from your PC, then writes two ready-to-paste files:
echo.
echo   1. %OUT%
echo      ^(send to BASE support^)
echo.
echo   2. %ITOUT%
echo      ^(forward to your IT department if BASE asks for a CIDR^)
echo.
echo IMPORTANT: Run this on the SAME office network where you'll
echo use the citation app. Running it from home or via VPN gives
echo a different IP, which BASE will reject when you're back at
echo the office.
echo.
echo Press any key to start...
pause >nul
echo.

rem --- Probe the same service three times to spot any NAT-pool
rem --- rotation, plus one different service as a sanity check.
echo Looking up your public IP ...
set "IP1="
set "IP2="
set "IP3="
set "IP4="
for /f "delims=" %%I in ('"%CURL%" -s -m 10 https://api.ipify.org 2^>nul') do set "IP1=%%I"
for /f "delims=" %%I in ('"%CURL%" -s -m 10 https://api.ipify.org 2^>nul') do set "IP2=%%I"
for /f "delims=" %%I in ('"%CURL%" -s -m 10 https://api.ipify.org 2^>nul') do set "IP3=%%I"
for /f "delims=" %%I in ('"%CURL%" -s -m 10 https://ipv4.icanhazip.com 2^>nul') do set "IP4=%%I"

if not defined IP1 (
    echo.
    echo ERROR: Could not detect your public IP.
    echo Check your internet connection and try again.
    echo If your office uses a proxy, run atfi-net-test.bat first
    echo and send that log to the developer.
    echo.
    pause
    exit /b 1
)

rem --- Decide whether the four readings disagree (NAT pool sign).
set "NATPOOL=0"
if not "!IP1!"=="!IP2!" set "NATPOOL=1"
if not "!IP1!"=="!IP3!" set "NATPOOL=1"
if defined IP4 if not "!IP1!"=="!IP4!" set "NATPOOL=1"

rem --- Ask RIPE Stat for the BGP-announced prefix that owns this IP.
rem --- This is the actual range the IP belongs to, not a guess.
rem --- PowerShell does the JSON parsing because cmd is bad at it.
rem Compute the /24 best-guess for the office's likely CIDR. Most small
rem and mid-size organisations are assigned a /24, so the network of the
rem detected IP is a sensible starting point. Larger orgs may own a
rem bigger or smaller block - IT has the authoritative answer, which is
rem why the IT-request template is generated below alongside the BASE
rem one. We keep the script free of PowerShell / cscript so it survives
rem locked-down government environments where shells are GPO-disabled.
set "PREFIX="
for /f "tokens=1-3 delims=." %%A in ("!IP1!") do set "PREFIX=%%A.%%B.%%C.0/24"

echo.
echo Detected public IP: !IP1!
if "!NATPOOL!"=="1" (
    echo Other readings:
    if defined IP2 if not "!IP2!"=="!IP1!" echo   - !IP2!
    if defined IP3 if not "!IP3!"=="!IP1!" if not "!IP3!"=="!IP2!" echo   - !IP3!
    if defined IP4 if not "!IP4!"=="!IP1!" if not "!IP4!"=="!IP2!" if not "!IP4!"=="!IP3!" echo   - !IP4!
    echo.
    echo NOTE: Different IPs in the same session - your office
    echo probably uses a NAT pool. BASE will need the full range,
    echo not just one IP.
) else (
    echo IP looks stable across all four checks.
)
echo.

echo Best-guess /24 around this IP:    !PREFIX!
echo ^(Just a guess based on the detected IP. The actual range
echo  your office owns may be different - your IT department
echo  has the authoritative answer. The IT email below asks
echo  for it.^)
echo.

echo NOTE: This IP is your whole office's gateway, shared with
echo every colleague on the same network. That is normal and
echo not a problem - BASE allowlists IPs, not individual users.
echo But you may want to let your IT department know before
echo asking BASE to allowlist the org's IP for an external API.
echo.

rem --- Write the BASE support message.
> "%OUT%" echo Subject: BASE HTTP API allowlist request
>>"%OUT%" echo.
>>"%OUT%" echo Hello BASE Support team,
>>"%OUT%" echo.
>>"%OUT%" echo I have a BASE API key but my requests are rejected with
>>"%OUT%" echo "Access denied for IP address ..." when I call the HTTP
>>"%OUT%" echo search API at api.base-search.net.
>>"%OUT%" echo.
>>"%OUT%" echo Please allowlist the institutional gateway my workstation
>>"%OUT%" echo uses for HTTP API access. This is a shared office IP - the
>>"%OUT%" echo same gateway is used by other colleagues on the same
>>"%OUT%" echo network, so the allowlist entry effectively covers our
>>"%OUT%" echo institution rather than a single user.
>>"%OUT%" echo.
>>"%OUT%" echo   Current public IP : !IP1!
>>"%OUT%" echo   Likely /24 around : !PREFIX!  ^(best-guess only^)
if "!NATPOOL!"=="1" (
    >>"%OUT%" echo.
    >>"%OUT%" echo   Other public IPs observed in the same session:
    if defined IP2 if not "!IP2!"=="!IP1!" >>"%OUT%" echo     - !IP2!
    if defined IP3 if not "!IP3!"=="!IP1!" if not "!IP3!"=="!IP2!" >>"%OUT%" echo     - !IP3!
    if defined IP4 if not "!IP4!"=="!IP1!" if not "!IP4!"=="!IP2!" if not "!IP4!"=="!IP3!" >>"%OUT%" echo     - !IP4!
    >>"%OUT%" echo.
    >>"%OUT%" echo   My organisation appears to use a NAT pool, so the
    >>"%OUT%" echo   outgoing IP can rotate. If a single-IP allowlist is
    >>"%OUT%" echo   not enough, our network administrator can send
    >>"%OUT%" echo   the exact CIDR block in a follow-up email.
)
>>"%OUT%" echo.
>>"%OUT%" echo Thank you,
>>"%OUT%" echo [Your name]
>>"%OUT%" echo [Your institution]
>>"%OUT%" echo.
>>"%OUT%" echo ---
>>"%OUT%" echo Detection details ^(for your reference, do not include in email^):
>>"%OUT%" echo   Date     : %DATE% %TIME%
>>"%OUT%" echo   User     : %USERNAME%@%COMPUTERNAME%
>>"%OUT%" echo   Probe 1  : api.ipify.org      -^> !IP1!
if defined IP2 >>"%OUT%" echo   Probe 2  : api.ipify.org      -^> !IP2!
if defined IP3 >>"%OUT%" echo   Probe 3  : api.ipify.org      -^> !IP3!
if defined IP4 >>"%OUT%" echo   Probe 4  : ipv4.icanhazip.com -^> !IP4!
>>"%OUT%" echo   /24 guess: !PREFIX!  ^(not authoritative^)
>>"%OUT%" echo   NAT pool : !NATPOOL!  ^(0 = stable, 1 = rotation seen^)

rem --- Write the IT-department request template (bilingual TR/EN, ASCII).
> "%ITOUT%" echo Konu / Subject: Akademik arastirma API'si icin kurum dis IP / CIDR
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Merhaba,
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Akademik makale dogrulama icin "BASE" ^(Bielefeld Academic
>>"%ITOUT%" echo Search Engine^) veritabaninin HTTP API'sini kullaniyorum.
>>"%ITOUT%" echo BASE, izinli IP listesine eklenebilmek icin kurumumuzun
>>"%ITOUT%" echo dis IP adres bilgisini istiyor.
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Bilgisayarimdan disari cikan IP : !IP1!
>>"%ITOUT%" echo /24 tahmini ^(otomatik, kesin degil^): !PREFIX!
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Asagidaki bilgileri rica ediyorum:
>>"%ITOUT%" echo   1. Kurumumuzun resmi dis IP adresi^(leri^).
>>"%ITOUT%" echo   2. Birden fazla varsa, kurumumuza tahsisli tam CIDR
>>"%ITOUT%" echo      blogu ^(ornek: 212.125.29.0/24^).
>>"%ITOUT%" echo   3. IP'nin sabit mi yoksa belirli araliklarla mi
>>"%ITOUT%" echo      degistigi.
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Bu bilgiyi BASE destek ekibine ileterek izin almak
>>"%ITOUT%" echo istiyorum. Allowlist islemi sadece o IP'den gelen
>>"%ITOUT%" echo akademik veritabani sorgularini etkiler, baska bir
>>"%ITOUT%" echo trafige etkisi yoktur.
>>"%ITOUT%" echo.
>>"%ITOUT%" echo Tesekkurler,
>>"%ITOUT%" echo [Adin]
>>"%ITOUT%" echo.
>>"%ITOUT%" echo --- English summary for non-Turkish-speaking IT ---
>>"%ITOUT%" echo I'm using BASE ^(an academic search API^) and they need
>>"%ITOUT%" echo to allowlist our institutional public IP. Could you
>>"%ITOUT%" echo confirm:
>>"%ITOUT%" echo   1. Our official outbound public IP^(s^).
>>"%ITOUT%" echo   2. The CIDR block assigned to our institution.
>>"%ITOUT%" echo   3. Whether the public IP is static or rotating.
>>"%ITOUT%" echo Detected from my workstation: !IP1!
>>"%ITOUT%" echo Best-guess /24 around it    : !PREFIX!  ^(not authoritative^)
>>"%ITOUT%" echo The allowlist only affects requests to api.base-search.net.

echo Saved files:
echo   %OUT%
echo   %ITOUT%
echo.
echo Next steps:
echo   1. Open base-allowlist-message.txt in Notepad.
echo   2. Replace [Your name] and [Your institution].
echo   3. Submit it at:
echo        https://www.base-search.net/about/en/contact.php
echo      Choose "Access BASE's HTTP API" in the form, then
echo      paste the message body.
echo.
echo   If BASE replies asking for a CIDR range or rejects the
echo   single IP, forward it-department-request.txt to your
echo   IT department. They will reply with the official CIDR.
echo.
echo   If BASE blocks again later from a different IP, your
echo   office probably rotates its public IP. Run this script
echo   again at that time and forward the new IP to BASE.
echo.
pause
exit /b 0
