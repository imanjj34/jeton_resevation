# Jeton Reservation

Jeton Reservation is a Windows desktop tool for reserving meals through the Yazd University Jeton system. It supports terminal and web interfaces, SSIM-based CAPTCHA assistance, menu downloading, saved selections, and bank payment windows.

## Features

- Login to the Jeton system
- Reserve meals through a terminal or local web interface
- Download menus as JSON
- Save pending selections
- Retry login and CAPTCHA recognition
- Open supported bank payment pages in a separate window
- Configure behavior through `config.ini`

## Requirements

- Windows 10 or newer
- Python 3.10 or newer
- Microsoft Edge WebView2 for web/payment interfaces
- Network access to `https://jeton.yazd.ac.ir/`

## Installation

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configuration

Run the setup wizard from the project directory:

```powershell
python src\jeton_menu.py --setup
```

This creates `config.ini`. You can also edit it manually; see [CONFIG.md](CONFIG.md) for every available option.

For credentials, environment variables are recommended instead of storing them in `config.ini`:

```powershell
$env:JETON_USER = "your-username"
$env:JETON_PASS = "your-password"
```

Environment variables take priority over values in `config.ini`.

## Run

Web interface:

```powershell
python src\jeton_menu.py --ui web
```

Terminal interface:

```powershell
python src\jeton_menu.py --ui terminal
```

Recreate or update the interactive configuration:

```powershell
python src\jeton_menu.py --setup
```

## Build a Windows executable

The project includes a PyInstaller specification and build script:

```powershell
.\build_app.ps1
```

The generated application is written to `dist\JetonReservation\`.

## Security

Do not commit `config.ini`, credentials, or pending reservation data. This repository ignores `config.ini`, `.env`, and `pending.json`.

The password field is stored as plain text if added to `config.ini`, so the environment-variable method is strongly recommended.

## License

This project is licensed for personal use only. See [LICENSE](LICENSE).
