"""Send WeChat message via AppleScript (macOS WeChat Desktop must be running).

PLATFORM-SPECIFIC: macOS only. This is an optional helper script that drives
the macOS WeChat Desktop application via AppleScript. It is NOT part of the
core QuantCairn selection pipeline or notification system. Not portable to
Linux or Windows. Use the Telegram notifier (src/notifier/alerts.py) for
cross-platform notifications.
"""
import subprocess, json, sys

def send_wechat(contact: str, message: str) -> bool:
    """Send a message to a WeChat contact using AppleScript.
    WeChat.app must be running and logged in."""
    script = f'''
    tell application "WeChat"
        activate
    end tell
    delay 0.5
    tell application "System Events"
        tell process "WeChat"
            set focused to true
            keystroke "f" using command down
            delay 0.3
            keystroke "{contact}"
            delay 0.5
            keystroke return
            delay 0.3
            keystroke "t" using command down
            delay 0.3
            keystroke "{message}"
            delay 0.3
            keystroke return
        end tell
    end tell
    '''
    try:
        subprocess.run(['osascript', '-e', script], capture_output=True, timeout=10)
        return True
    except Exception as e:
        print(f'WeChat send error: {e}', file=sys.stderr)
        return False

if __name__ == '__main__':
    contact = sys.argv[1] if len(sys.argv) > 1 else '文件传输助手'
    msg = sys.argv[2] if len(sys.argv) > 2 else 'test'
    ok = send_wechat(contact, msg)
    print('OK' if ok else 'FAIL')
