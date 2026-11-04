@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ============================================================
rem  AtfiMemnu network diagnostic
rem  Double-click to run. Works on Windows 10 1803+ / Windows 11.
rem  Produces atfi-net-test.log next to this file.
rem ============================================================

set "LOG=%~dp0atfi-net-test.log"
set "CURL=%SystemRoot%\System32\curl.exe"

if not exist "%CURL%" (
    echo curl.exe not found at %CURL%
    echo Your Windows is too old ^(needs 10 build 1803 or newer^).
    pause
    exit /b 1
)

> "%LOG%" echo === AtfiMemnu network diagnostic ===
>>"%LOG%" echo Date: %DATE% %TIME%
>>"%LOG%" echo Computer: %COMPUTERNAME%
>>"%LOG%" echo User: %USERNAME%
>>"%LOG%" echo.

>>"%LOG%" echo --- System proxy (netsh winhttp) ---
netsh winhttp show proxy >> "%LOG%" 2>&1
>>"%LOG%" echo.

>>"%LOG%" echo --- Environment proxy variables ---
>>"%LOG%" echo HTTP_PROXY=%HTTP_PROXY%
>>"%LOG%" echo HTTPS_PROXY=%HTTPS_PROXY%
>>"%LOG%" echo NO_PROXY=%NO_PROXY%
>>"%LOG%" echo.

call :test "Crossref"        "https://api.crossref.org/works/10.1038/nature12373"
call :test "arXiv (HTTP)"    "http://export.arxiv.org/api/query?search_query=ti:test&max_results=1"
call :test "Semantic Scholar" "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1"
call :test "OpenAlex"        "https://api.openalex.org/works?search=test&per-page=1"
call :test "Europe PMC"      "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=test&format=json&pageSize=1"
call :test "PubMed (NCBI)"   "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=test&retmax=1&retmode=json"
call :test "OpenAIRE"        "https://api.openaire.eu/graph/v2/researchProducts?search=test&pageSize=1"
call :test "Open Library"    "https://openlibrary.org/search.json?q=test&limit=1"
call :test "TR Dizin"        "https://search.trdizin.gov.tr/api/defaultSearch/publication/?q=test&limit=1"
call :test "Web of Science"  "https://api.clarivate.com/apis/wos-starter/v2/documents?q=DO=10.1038/nature12373&limit=1"
call :test "BASE"            "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi?func=PerformSearch&query=test&hits=1"

>>"%LOG%" echo.
>>"%LOG%" echo --- DNS resolution ---
for %%H in (api.crossref.org export.arxiv.org api.semanticscholar.org api.openalex.org www.ebi.ac.uk eutils.ncbi.nlm.nih.gov api.openaire.eu openlibrary.org search.trdizin.gov.tr api.clarivate.com api.base-search.net) do (
    >>"%LOG%" echo.
    >>"%LOG%" echo [nslookup %%H]
    nslookup %%H >> "%LOG%" 2>&1
)

echo.
echo Done. Results saved to:
echo   %LOG%
echo.
echo Please send that file back.
echo.
pause
exit /b 0


:test
set "NAME=%~1"
set "URL=%~2"
>>"%LOG%" echo.
>>"%LOG%" echo ============================================================
>>"%LOG%" echo  TEST: %NAME%
>>"%LOG%" echo  URL : %URL%
>>"%LOG%" echo ============================================================
echo Testing %NAME% ...

rem -s silent body, -S show errors, -v verbose to capture TLS handshake,
rem -o NUL discard body, -w summary line, --max-time 20s
"%CURL%" -sS -v -o nul ^
    --max-time 20 ^
    -A "AtfiMemnu/2.14 (diagnostic)" ^
    -w "HTTP_STATUS=%%{http_code}  TIME=%%{time_total}s  IP=%%{remote_ip}  SSL_VERIFY=%%{ssl_verify_result}\n" ^
    "%URL%" >> "%LOG%" 2>&1

if errorlevel 1 (
    >>"%LOG%" echo RESULT: FAIL ^(curl exit code %errorlevel%^)
) else (
    >>"%LOG%" echo RESULT: OK
)
exit /b 0
