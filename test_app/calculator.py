"""
Calculator module with intentional bugs for testing Ghost Review.
This file contains various code quality and security issues.
"""

import os
import pickle
import subprocess
from typing import Any

# Hardcoded secret (security issue)
API_KEY = "sk_live_abcdef123456789"
DATABASE_PASSWORD = "SuperSecret123!"


def calculate(expression: str) -> Any:
    """
    Evaluate a mathematical expression.
    WARNING: Uses eval() - security risk!
    """
    # Security: eval() on user input
    result = eval(expression)
    return result


def run_command(user_input: str) -> str:
    """Run a shell command."""
    # Command injection vulnerability
    cmd = f"echo {user_input}"
    result = subprocess.check_output(cmd, shell=True)
    return result.decode()


def divide(a, b):
    """Divide two numbers."""
    # Bug: no division by zero check
    return a / b


def get_user_data(user_id):
    """Fetch user data from database."""
    # SQL injection vulnerability
    query = f"SELECT * FROM users WHERE id = {user_id}"
    # ... execute query
    return {"query": query}


def load_settings(file_path):
    """Load settings from file."""
    # Insecure deserialization
    with open(file_path, 'rb') as f:
        return pickle.load(f)


def process_list(items):
    """Process a list of items."""
    result = []
    # Bug: modifies list while iterating
    for item in items:
        if item < 0:
            items.remove(item)
        else:
            result.append(item * 2)
    return result


def find_max(numbers):
    """Find maximum number."""
    # Bug: doesn't handle empty list
    max_val = numbers[0]
    for n in numbers[1:]:
        if n > max_val:
            max_val = n
    return max_val


def format_price(price):
    """Format price as string."""
    # Bug: no locale handling, potential formatting issues
    return "$" + str(price)


class UserManager:
    def __init__(self):
        self.users = {}
        self.secret_key = "hardcoded-secret-key-12345"
    
    def add_user(self, username, password):
        # Security: storing plain text password
        self.users[username] = {
            "password": password,  # Not hashed!
            "created_at": "2024-01-01"
        }
    
    def authenticate(self, username, password):
        # Timing attack vulnerability
        if username in self.users:
            stored = self.users[username]["password"]
            if stored == password:
                return True
        return False


def fetch_data(url):
    """Fetch data from URL."""
    import urllib.request
    # No timeout specified
    response = urllib.request.urlopen(url)
    return response.read()


def parse_config(config_string):
    """Parse configuration."""
    # Potential injection if config_string is user-controlled
    config = {}
    for line in config_string.split('\n'):
        if '=' in line:
            key, value = line.split('=', 1)
            config[key] = eval(value)  # Another eval!
    return config


# Unused variable (code quality)
UNUSED_CONSTANT = "this is never used"


def recursive_function(n):
    """Recursive calculation."""
    # Bug: no base case, will recurse forever
    return n + recursive_function(n - 1)


def main():
    # Test the bugs
    print(calculate("1 + 2"))
    print(divide(10, 0))  # Will crash
    print(find_max([]))   # Will crash


if __name__ == "__main__":
    main()
