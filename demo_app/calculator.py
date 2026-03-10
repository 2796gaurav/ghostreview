"""Calculator module with intentional bugs for testing."""


def divide(a, b):
    """Divide two numbers - has zero division bug."""
    return a / b


def calculate_average(numbers):
    """Calculate average - has empty list bug."""
    return sum(numbers) / len(numbers)
