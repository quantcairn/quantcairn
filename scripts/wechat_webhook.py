"""Webhook HTTP server that forwards trading notifications to WeChat"""
import subprocess, json, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

CONTACT = "文件传输助手"  # 改成你的微信名字

class WeChatHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            embed = data.get('embeds', [{}])[0]
            title = embed.get('title', '')
            desc = embed.get('description', '')
            msg = f"{title}\n{desc}" if title or desc else "交易通知"
            self._send(msg)
        except Exception as e:
            self._send(f"Trade alert: {body[:200]}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'WeChat relay running')
    
    def _send(self, msg):
        safe_msg = msg.replace('"', "'").replace('\\', '')
        script = f'''
        tell application "WeChat"
            activate
        end tell
        delay 0.3
        tell application "System Events"
            tell process "WeChat"
                set focused to true
                keystroke "f" using command down
                delay 0.2
                keystroke "{CONTACT}"
                delay 0.4
                keystroke return
                delay 0.3
                keystroke "t" using command down
                delay 0.2
                keystroke "{safe_msg}"
                delay 0.2
                keystroke return
            end tell
        end tell
        '''
        try:
            subprocess.run(['osascript', '-e', script], capture_output=True, timeout=15)
        except:
            pass

if __name__ == '__main__':
    port = 18901
    server = HTTPServer(('0.0.0.0', port), WeChatHandler)
    print(f'WeChat relay on :{port}')
    server.serve_forever()
