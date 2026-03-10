"""
Test file with intentional bugs for Ghost Review to detect.
This simulates a simple API with security issues.
"""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer


class APIHandler(BaseHTTPRequestHandler):
    """Simple API handler with intentional vulnerabilities."""
    
    DB_PATH = "app.db"
    API_KEY = "sk_test_1234567890abcdef_secret_key"  # Hardcoded secret
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path.startswith("/user/"):
            user_id = self.path.split("/")[-1]
            self.get_user(user_id)
        else:
            self.send_error(404)
    
    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/login":
            self.login()
        elif self.path == "/calculate":
            self.calculate()
        else:
            self.send_error(404)
    
    def get_user(self, user_id):
        """Get user by ID - SQL Injection vulnerability."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        # CRITICAL: SQL Injection vulnerability
        query = f"SELECT * FROM users WHERE id = '{user_id}'"
        cursor.execute(query)
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"user": user}).encode())
        else:
            self.send_error(404)
    
    def login(self):
        """User login - multiple issues."""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        # Missing input validation
        data = json.loads(post_data)
        username = data.get('username', '')
        password = data.get('password', '')
        
        # Weak: Timing attack possible
        if username == "admin" and password == "password123":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({
                "token": "super_secret_token_12345",
                "api_key": self.API_KEY  # Leaking secret
            }).encode())
        else:
            self.send_error(401)
    
    def calculate(self):
        """Calculate division - ZeroDivisionError bug."""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        data = json.loads(post_data)
        a = data.get('a', 0)
        b = data.get('b', 0)
        
        # BUG: No check for division by zero
        result = a / b
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"result": result}).encode())


def run_server(port=8080):
    """Run the API server."""
    server = HTTPServer(('0.0.0.0', port), APIHandler)
    print(f"Server running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
