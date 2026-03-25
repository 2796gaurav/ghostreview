"""Sample code with bugs for testing."""

# Hardcoded secret
API_KEY = "sk_live_1234567890abcdef"

def divide(a, b):
    # Bug: division by zero
    return a / b

def calculate(expr):
    # Security: eval on user input
    return eval(expr)
