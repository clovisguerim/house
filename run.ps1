param([int]$Port = 8000)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    & $py -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port
