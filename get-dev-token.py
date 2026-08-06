# You can run this directly in a Python shell or save it as `auth_setup.py` and run it:
import http.server, threading, webbrowser, pathlib

token_result = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        token_result["token"] = qs.get("token", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h1>Done! You can close this tab.</h1>")
        threading.Thread(target=self.server.shutdown).start()
    def log_message(self, *args): pass

server = http.server.HTTPServer(("localhost", 0), Handler)
port = server.server_address[1]

# Opens your browser to authenticate
webbrowser.open(
    f"https://auth-service.storesight.org/dev-token?callback=http://localhost:{port}/callback"
)
server.serve_forever()

# Saves the token to ~/.storesight/dev-token
if token_result.get("token"):
    p = pathlib.Path.home() / ".storesight"
    p.mkdir(mode=0o700, exist_ok=True)
    tok_file = p / "dev-token"
    tok_file.write_text(token_result["token"])
    tok_file.chmod(0o600)
    print("Authentication successful! Token saved.")
