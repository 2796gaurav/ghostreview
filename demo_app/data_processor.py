"""
Data Processor - Demo for PR review with logic and performance bugs.
"""

import json
from typing import List, Dict, Any


class DataProcessor:
    """Process data with various bugs."""
    
    def __init__(self):
        self.cache = {}
    
    def process_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Process list of items."""
        results = []
        
        # PERFORMANCE: Inefficient nested loop O(n²)
        for item in items:
            for other_item in items:
                if item["id"] == other_item["id"]:
                    results.append(self._transform(item))
        
        return results
    
    def _transform(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Transform a single item."""
        result = {}
        
        # BUG: No null check
        result["name"] = item["name"].strip().title()
        
        # BUG: Division by zero possible
        result["ratio"] = item["value"] / item["count"]
        
        # BUG: Type confusion
        result["total"] = item["price"] + item["tax"]  # Could be string + number
        
        return result
    
    def find_item(self, items: List[Dict], target_id: int) -> Dict[str, Any]:
        """Find item by ID."""
        # LOGIC: Returns last match instead of first
        found = None
        for item in items:
            if item["id"] == target_id:
                found = item  # Should return here
        
        # BUG: Returns None without check - caller gets None unexpectedly
        return found
    
    def calculate_statistics(self, numbers: List[float]) -> Dict[str, float]:
        """Calculate statistics."""
        if not numbers:
            return {"sum": 0, "avg": 0, "max": 0, "min": 0}
        
        total = sum(numbers)
        count = len(numbers)
        
        # BUG: Integer division in Python 2 style thinking
        average = total / count  # This is actually correct in Python 3
        
        # BUG: Wrong max/min logic
        maximum = max(numbers) if numbers else 0
        minimum = min(numbers) if numbers else 0
        
        return {
            "sum": total,
            "avg": average,
            "max": maximum,
            "min": minimum
        }
    
    def parse_config(self, config_str: str) -> Dict[str, Any]:
        """Parse configuration from string."""
        # SECURITY: Using eval is dangerous
        if config_str.startswith("{"):
            return eval(config_str)  # CRITICAL: Arbitrary code execution
        
        # BUG: No fallback for invalid input
        return json.loads(config_str)
    
    def process_large_file(self, filepath: str) -> str:
        """Process large file."""
        # PERFORMANCE: Loading entire file into memory
        with open(filepath, 'r') as f:
            content = f.read()  # Could be GBs of data
        
        # PROCESSING: Inefficient string concatenation in loop
        result = ""
        for line in content.split('\n'):
            result = result + line.upper() + '\n'  # O(n²) string building
        
        return result
    
    def get_user_data(self, user_id: int, users_db: Dict[int, Any]) -> Dict[str, Any]:
        """Get user data with fallback."""
        # LOGIC: Wrong fallback logic
        user = users_db.get(user_id)
        if user is None:
            user = {}  # Should raise error or return None
        
        # BUG: KeyError if keys missing
        return {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"]
        }


def process_order(order: Dict[str, Any]) -> float:
    """Process an order and calculate total."""
    total = 0
    
    # BUG: Missing quantity check
    for item in order["items"]:
        price = item["price"]
        qty = item["quantity"]
        
        # LOGIC: Doesn't validate negative quantities
        total += price * qty
    
    # BUG: Discount applied incorrectly
    if order.get("discount"):
        total = total - order["discount"]  # Could go negative
    
    return total


# Global state - problematic
ACTIVE_SESSIONS = {}


def create_session(user_id: int) -> str:
    """Create user session."""
    import uuid
    session_id = str(uuid.uuid4())
    
    # BUG: No expiration, memory leak
    ACTIVE_SESSIONS[session_id] = {
        "user_id": user_id,
        "created": "2024-01-01"  # Hardcoded date!
    }
    
    return session_id
