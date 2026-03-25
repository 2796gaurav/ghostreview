# Test Application for Ghost Review

This directory contains intentionally buggy code for testing the Ghost Review PR analysis feature.

## Purpose

The `calculator.py` file contains various security vulnerabilities and bugs that Ghost Review should detect:

### Security Issues
1. **Hardcoded Secrets** - API keys and passwords in source code
2. **eval() Injection** - Code execution via user input
3. **SQL Injection** - Unparameterized database queries
4. **Command Injection** - Unsafe subprocess usage with shell=True
5. **Insecure Deserialization** - Using pickle.load() on untrusted data
6. **Plaintext Passwords** - Storing passwords without hashing

### Bugs
1. **Division by Zero** - No zero check in divide()
2. **Empty List Handling** - find_max() crashes on empty list
3. **List Modification During Iteration** - process_list() modifies while iterating
4. **Infinite Recursion** - recursive_function() has no base case
5. **Missing Timeouts** - fetch_data() can hang indefinitely

## How to Test

Create a PR from this branch to main. Ghost Review should analyze the code and report these issues in a PR comment.
