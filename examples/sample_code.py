"""
Sample code to test Ghost Review PR analysis.
This file contains intentional issues for testing.
"""

import os
import subprocess


def calculate_average(numbers):
    """Calculate average of a list of numbers."""
    # BUG: No check for empty list - will cause ZeroDivisionError
    total = sum(numbers)
    return total / len(numbers)


def get_user_data(user_id):
    """Fetch user data from database."""
    # SECURITY: SQL injection vulnerability
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return execute_query(query)


def execute_query(sql):
    """Execute SQL query (mock)."""
    # Placeholder for actual database call
    return {"query": sql}


def run_command(user_input):
    """Run a system command."""
    # CRITICAL: Command injection vulnerability
    cmd = f"echo {user_input}"
    result = subprocess.run(cmd, shell=True, capture_output=True)
    return result.stdout.decode()


def process_payment(amount, card_number):
    """Process a payment."""
    # SECURITY: Hardcoded API key
    api_key = "sk_live_1234567890abcdef"
    
    # Missing validation
    charge = {
        "amount": amount,
        "card": card_number,
        "api_key": api_key
    }
    return charge


def divide_numbers(a, b):
    """Divide two numbers."""
    # BUG: No zero division check
    return a / b


def load_config():
    """Load configuration."""
    # Loading from current directory without validation
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r") as f:
        return f.read()


class UserManager:
    """Manage user operations."""
    
    def __init__(self):
        self.users = {}
        self.secret_key = "my_super_secret_key_12345"  # Hardcoded secret
    
    def add_user(self, username, password):
        """Add a new user."""
        # WEAK: Storing plaintext password
        self.users[username] = {
            "password": password,  # Should be hashed!
            "created_at": "2024-01-01"
        }
        return True
    
    def authenticate(self, username, password):
        """Authenticate user."""
        # Logic flaw - no rate limiting
        user = self.users.get(username)
        if user and user["password"] == password:
            return True
        return False


# Example usage
if __name__ == "__main__":
    manager = UserManager()
    manager.add_user("admin", "password123")
    
    # Test the functions
    avg = calculate_average([1, 2, 3, 4, 5])
    print(f"Average: {avg}")
