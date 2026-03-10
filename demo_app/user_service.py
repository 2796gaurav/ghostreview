"""
User Service - Demo application for Ghost Review PR testing.
This file contains intentional security issues for demonstration.
"""

import sqlite3
import hashlib
import jwt
from datetime import datetime, timedelta
from typing import Optional, Dict, Any


class UserService:
    """Service for user authentication and management."""
    
    # CRITICAL: Hardcoded secret key
    JWT_SECRET = "my_super_secret_key_12345_do_not_change"
    DB_PATH = "users.db"
    
    def __init__(self):
        self._init_db()
    
    def _init_db(self):
        """Initialize database."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE,
                password TEXT,
                email TEXT,
                is_admin INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
    
    def register_user(self, username: str, password: str, email: str) -> Dict[str, Any]:
        """Register a new user."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        # SECURITY: SQL Injection vulnerability
        query = f"INSERT INTO users (username, password, email) VALUES ('{username}', '{password}', '{email}')"
        cursor.execute(query)
        
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        return {"id": user_id, "username": username}
    
    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Authenticate user and return JWT token."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        # SECURITY: SQL Injection in login
        query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
        cursor.execute(query)
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            # Generate JWT token
            payload = {
                "user_id": user[0],
                "username": user[1],
                "is_admin": user[4],
                "exp": datetime.utcnow() + timedelta(hours=24)
            }
            token = jwt.encode(payload, self.JWT_SECRET, algorithm="HS256")
            return token
        
        return None
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by ID."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        # SECURITY: SQL Injection
        query = f"SELECT * FROM users WHERE id = {user_id}"
        cursor.execute(query)
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            return {
                "id": user[0],
                "username": user[1],
                "email": user[3],
                "is_admin": bool(user[4])
            }
        return None
    
    def update_password(self, user_id: int, new_password: str) -> bool:
        """Update user password."""
        # WEAK: No password strength validation
        # WEAK: Storing plaintext password
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        query = f"UPDATE users SET password = '{new_password}' WHERE id = {user_id}"
        cursor.execute(query)
        
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        
        return success
    
    def delete_user(self, user_id: int) -> bool:
        """Delete user by ID."""
        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        # SECURITY: SQL Injection
        query = f"DELETE FROM users WHERE id = {user_id}"
        cursor.execute(query)
        
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        
        return success
    
    def calculate_average(self, numbers: list) -> float:
        """Calculate average of numbers."""
        # BUG: No check for empty list - ZeroDivisionError
        total = sum(numbers)
        return total / len(numbers)
    
    def process_user_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process user data."""
        result = {}
        
        # Logic flaw: No input validation
        result["name"] = data["name"].upper()
        result["email"] = data["email"].lower()
        
        # BUG: KeyError if age not present
        age = data["age"]
        if age < 0:
            raise ValueError("Age cannot be negative")
        
        result["age"] = age
        return result


def create_admin_user():
    """Create default admin user."""
    service = UserService()
    # CRITICAL: Hardcoded admin credentials
    service.register_user("admin", "admin123", "admin@example.com")
    return service


if __name__ == "__main__":
    service = create_admin_user()
    print("Admin user created")
