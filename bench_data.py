"""Benchmark task suites for the ARC LLM bench harness.

Three suites:
- "humaneval": canonical HumanEval-style function tasks (MIT-licensed subset,
  openai/human-eval) — sanity baseline; known saturated for frontier models.
- "original": hand-written function tasks created for this repo (2026) with
  tricky-but-deterministic edge cases — contamination-resistant signal.
- "package": multi-file mini-project specs with pre-written tests, meant for
  CLI harnesses (opencode/kimi) and the multi-block direct harness.

Every task is a dict:
  task_id   unique slug
  suite     suite name
  tier      easy | medium | hard
  kind      "function" | "package"
  entry     name of the target function (function tasks)
  prompt    full spec text shown to the model
  files     {path: content} pre-written into the workdir (tests, fixtures)
  timeout   per-test-file subprocess timeout in seconds
"""
import textwrap

_TEST_TIMEOUT = 20


def _dedent(s):
    return textwrap.dedent(s).strip() + "\n"


def _ft(task_id, tier, entry, prompt, tests, suite):
    return {
        "task_id": task_id,
        "suite": suite,
        "tier": tier,
        "kind": "function",
        "entry": entry,
        "prompt": _dedent(prompt),
        "files": {"test_task.py": _dedent(tests)},
        "timeout": _TEST_TIMEOUT,
    }


# ---------------------------------------------------------------------------
# humaneval suite — canonical HumanEval function tasks (subset, easy->hard).
# Adapted from openai/human-eval (MIT license). Tests are plain asserts on an
# imported `solution` module so no pytest dependency is needed.
# ---------------------------------------------------------------------------

HUMANEVAL = [
    _ft(
        "he-strlen", "easy", "strlen",
        '''
        def strlen(string: str) -> int:
            """Return length of given string
            >>> strlen('')
            0
            >>> strlen('abc')
            3
            """
        ''',
        '''
        import solution
        assert solution.strlen("") == 0
        assert solution.strlen("x") == 1
        assert solution.strlen("asdasnakj") == 9
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-truncate_number", "easy", "truncate_number",
        '''
        def truncate_number(number: float) -> float:
            """Given a positive floating point number, it can be decomposed into
            an integer part (largest integer smaller than the given number) and decimals
            (leftover part always smaller than 1).

            Return the decimal part of the number.
            >>> truncate_number(3.5)
            0.5
            """
        ''',
        '''
        import math
        import solution
        assert math.isclose(solution.truncate_number(3.5), 0.5, abs_tol=1e-6)
        assert math.isclose(solution.truncate_number(1.33), 0.33, abs_tol=1e-6)
        assert math.isclose(solution.truncate_number(123.456), 0.456, abs_tol=1e-6)
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-below_threshold", "easy", "below_threshold",
        '''
        def below_threshold(l: list, t: int):
            """Return True if all numbers in the list l are below threshold t.
            >>> below_threshold([1, 2, 4, 10], 100)
            True
            >>> below_threshold([1, 20, 4, 10], 5)
            False
            """
        ''',
        '''
        import solution
        assert solution.below_threshold([1, 2, 4, 10], 100) is True
        assert solution.below_threshold([1, 20, 4, 10], 5) is False
        assert solution.below_threshold([1, 20, 4, 10], 21) is True
        assert solution.below_threshold([1, 20, 4, 10], 22) is True
        assert solution.below_threshold([1, 8, 4, 10], 11) is True
        assert solution.below_threshold([1, 8, 4, 10], 10) is False
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-concatenate", "easy", "concatenate",
        '''
        from typing import List


        def concatenate(strings: List[str]) -> str:
            """Concatenate list of strings into a single string
            >>> concatenate([])
            ''
            >>> concatenate(['a', 'b', 'c'])
            'abc'
            """
        ''',
        '''
        import solution
        assert solution.concatenate([]) == ""
        assert solution.concatenate(["x", "y", "z"]) == "xyz"
        assert solution.concatenate(["x", "y", "z", "w", "k"]) == "xyzwk"
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-sum_to_n", "easy", "sum_to_n",
        '''
        def sum_to_n(n: int):
            """sum_to_n is a function that sums numbers from 1 to n.
            >>> sum_to_n(30)
            465
            >>> sum_to_n(100)
            5050
            """
        ''',
        '''
        import solution
        assert solution.sum_to_n(1) == 1
        assert solution.sum_to_n(6) == 21
        assert solution.sum_to_n(11) == 66
        assert solution.sum_to_n(30) == 465
        assert solution.sum_to_n(100) == 5050
        print("ok")
        ''',
        "humaneval",
    ),
]

HUMANEVAL += [
    _ft(
        "he-is_palindrome", "medium", "is_palindrome",
        '''
        def is_palindrome(string: str) -> bool:
            """Checks if given string is a palindrome
            >>> is_palindrome('')
            True
            >>> is_palindrome('aba')
            True
            >>> is_palindrome('aaaaa')
            True
            >>> is_palindrome('zbcd')
            False
            """
        ''',
        '''
        import solution
        assert solution.is_palindrome("") is True
        assert solution.is_palindrome("aba") is True
        assert solution.is_palindrome("aaaaa") is True
        assert solution.is_palindrome("zbcd") is False
        assert solution.is_palindrome("xywyx") is True
        assert solution.is_palindrome("xywyz") is False
        assert solution.is_palindrome("xywzx") is False
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-factorize", "medium", "factorize",
        '''
        from typing import List


        def factorize(n: int) -> List[int]:
            """Return list of prime factors of given integer in the order from smallest to largest.
            Each of the factors should be listed number of times corresponding to how many times it appears in factorization.
            Input number should be equal to the product of all factors
            >>> factorize(8)
            [2, 2, 2]
            >>> factorize(25)
            [5, 5]
            >>> factorize(70)
            [2, 5, 7]
            """
        ''',
        '''
        import solution
        assert solution.factorize(2) == [2]
        assert solution.factorize(4) == [2, 2]
        assert solution.factorize(8) == [2, 2, 2]
        assert solution.factorize(57) == [3, 19]
        assert solution.factorize(3249) == [3, 3, 19, 19]
        assert solution.factorize(185193) == [3, 3, 3, 19, 19, 19]
        assert solution.factorize(20577) == [3, 19, 19, 19]
        assert solution.factorize(18) == [2, 3, 3]
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-fib", "medium", "fib",
        '''
        def fib(n: int):
            """Return n-th Fibonacci number.
            >>> fib(10)
            55
            >>> fib(1)
            1
            >>> fib(8)
            21
            """
        ''',
        '''
        import solution
        assert solution.fib(1) == 1
        assert solution.fib(2) == 1
        assert solution.fib(8) == 21
        assert solution.fib(10) == 55
        assert solution.fib(12) == 144
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-pairs_sum_to_zero", "medium", "pairs_sum_to_zero",
        '''
        def pairs_sum_to_zero(l):
            """pairs_sum_to_zero takes a list of integers as an input.
            it returns True if there are two distinct elements in the list that
            sum to zero, and False otherwise.
            >>> pairs_sum_to_zero([1, 3, 5, 0])
            False
            >>> pairs_sum_to_zero([1, 3, -2, 1])
            False
            >>> pairs_sum_to_zero([1, 2, 3, 7])
            False
            >>> pairs_sum_to_zero([2, 4, -5, 3, 5, 7])
            True
            >>> pairs_sum_to_zero([1])
            False
            """
        ''',
        '''
        import solution
        assert solution.pairs_sum_to_zero([1, 3, 5, 0]) is False
        assert solution.pairs_sum_to_zero([1, 3, -2, 1]) is False
        assert solution.pairs_sum_to_zero([1, 2, 3, 7]) is False
        assert solution.pairs_sum_to_zero([2, 4, -5, 3, 5, 7]) is True
        assert solution.pairs_sum_to_zero([1]) is False
        assert solution.pairs_sum_to_zero([-3, 9, -1, 3, 2, 30]) is True
        assert solution.pairs_sum_to_zero([-3, 9, -1, 3, 2, 31]) is True
        assert solution.pairs_sum_to_zero([-3, 9, -1, 4, 2, 30]) is False
        assert solution.pairs_sum_to_zero([-3, 9, -1, 4, 2, 31]) is False
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-common", "medium", "common",
        '''
        def common(l1: list, l2: list):
            """Return sorted unique common elements for two lists.
            >>> common([1, 4, 3, 34, 653, 2, 5], [5, 7, 1, 5, 9, 653, 121])
            [1, 5, 653]
            >>> common([5, 3, 2, 8], [3, 2])
            [2, 3]
            """
        ''',
        '''
        import solution
        assert solution.common([1, 4, 3, 34, 653, 2, 5], [5, 7, 1, 5, 9, 653, 121]) == [1, 5, 653]
        assert solution.common([5, 3, 2, 8], [3, 2]) == [2, 3]
        assert solution.common([4, 3, 2, 8], [3, 2, 4]) == [2, 3, 4]
        assert solution.common([4, 3, 2, 8], []) == []
        print("ok")
        ''',
        "humaneval",
    ),
]

HUMANEVAL += [
    _ft(
        "he-fibfib", "hard", "fibfib",
        '''
        def fibfib(n: int):
            """The FibFib number sequence is a sequence similar to the Fibonacci sequence
            that is defined as follows:
            fibfib(0) == 0
            fibfib(1) == 0
            fibfib(2) == 1
            fibfib(n) == fibfib(n-1) + fibfib(n-2) + fibfib(n-3).
            Please write a function to efficiently compute the n-th element of the fibfib number sequence.
            >>> fibfib(1)
            0
            >>> fibfib(5)
            4
            >>> fibfib(8)
            24
            """
        ''',
        '''
        import solution
        assert solution.fibfib(2) == 1
        assert solution.fibfib(1) == 0
        assert solution.fibfib(5) == 4
        assert solution.fibfib(8) == 24
        assert solution.fibfib(10) == 81
        assert solution.fibfib(12) == 274
        assert solution.fibfib(14) == 927
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-hex_key", "hard", "hex_key",
        '''
        def hex_key(num):
            """You have been tasked to write a function that receives
            a hexadecimal number as a string and counts the number of hexadecimal
            digits that are primes (prime number, or a prime, is a natural number
            greater than 1 that is not a product of two smaller natural numbers).
            Hexadecimal digits are 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, A, B, C, D, E, F.
            Prime numbers are 2, 3, 5, 7, 11, 13, 17,...
            So you have to determine a number of the following digits: 2, 3, 5, 7,
            B (=decimal 11), D (=decimal 13).
            Note: you may assume the input is always correct or empty string,
            and symbols A,B,C,D,E,F are always uppercase.
            Examples:
            For num = "AB" the result should be 1.
            For num = "1077E" the result should be 2.
            For num = "ABED1A33" the result should be 4.
            For num = "123456789ABCDEF0" the result should be 6.
            For num = "2020" the result should be 2.
            """
        ''',
        '''
        import solution
        assert solution.hex_key("AB") == 1
        assert solution.hex_key("1077E") == 2
        assert solution.hex_key("ABED1A33") == 4
        assert solution.hex_key("2020") == 2
        assert solution.hex_key("123456789ABCDEF0") == 6
        assert solution.hex_key("112233445566778899AABBCCDDEEFF00") == 12
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-sort_array", "hard", "sort_array",
        '''
        def sort_array(array):
            """Given an array of non-negative integers, return a copy of the given array
            after sorting, you will sort the given array in ascending order if the
            sum(first index value, last index value) is odd, or sort it in descending
            order if the sum(first index value, last index value) is even.

            Note:
            * don't change the given array.

            Examples:
            * sort_array([]) => []
            * sort_array([5]) => [5]
            * sort_array([2, 4, 3, 0, 1, 5]) => [0, 1, 2, 3, 4, 5]
            * sort_array([2, 4, 3, 0, 1, 5, 6]) => [6, 5, 4, 3, 2, 1, 0]
            """
        ''',
        '''
        import solution
        assert solution.sort_array([]) == []
        assert solution.sort_array([5]) == [5]
        assert solution.sort_array([11, 3]) == [11, 3]
        assert solution.sort_array([2, 4, 3, 0, 1, 5]) == [0, 1, 2, 3, 4, 5]
        assert solution.sort_array([2, 4, 3, 0, 1, 5, 6]) == [6, 5, 4, 3, 2, 1, 0]
        assert solution.sort_array([2, 1]) == [1, 2]
        assert solution.sort_array([15, 42, 87, 32, 11, 0]) == [0, 11, 15, 32, 42, 87]
        assert solution.sort_array([21, 14, 23, 11]) == [23, 21, 14, 11]
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-get_row", "hard", "get_row",
        '''
        def get_row(lst, x):
            """You are given a 2 dimensional data, as a nested lists,
            which is similar to matrix, however, unlike matrices,
            each row may contain a different number of columns.
            Given lst, and integer x, find integers x in the list,
            and return list of tuples, [(x1, y1), (x2, y2) ...] such that
            each tuple is a coordinate - (row, column), starting with 0.
            Sort coordinates initially by rows in ascending order.
            Also, sort coordinates of the row by columns in descending order.

            Examples:
            get_row([
              [1,2,3,4,5,6],
              [1,2,3,4,1,6],
              [1,2,3,4,5,1]
            ], 1) == [(0, 0), (1, 4), (1, 0), (2, 5), (2, 0)]
            get_row([], 1) == []
            get_row([[], [1], [1, 2, 3]], 3) == [(2, 2)]
            """
        ''',
        '''
        import solution
        assert solution.get_row([
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 1, 6],
            [1, 2, 3, 4, 5, 1],
        ], 1) == [(0, 0), (1, 4), (1, 0), (2, 5), (2, 0)]
        assert solution.get_row([
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [2, 2, 3, 4, 5, 6],
        ], 1) == [(0, 0), (1, 0), (2, 0), (3, 0)]
        assert solution.get_row([
            [2, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [1, 2, 3, 4, 5, 6],
            [2, 2, 3, 4, 5, 6],
        ], 2) == [(0, 1), (0, 0), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (5, 0)]
        assert solution.get_row([], 1) == []
        assert solution.get_row([[], [1], [1, 2, 3]], 3) == [(2, 2)]
        print("ok")
        ''',
        "humaneval",
    ),
    _ft(
        "he-check_dict_case", "hard", "check_dict_case",
        '''
        def check_dict_case(dict):
            """Given a dictionary, return True if all keys are strings in lower
            case or all keys are strings in upper case, else return False.
            The function should return False if the given dictionary is empty.
            Examples:
            check_dict_case({"a":"apple", "b":"banana"}) should return True.
            check_dict_case({"a":"apple", "A":"banana", "B":"banana"}) should return False.
            check_dict_case({"a":"apple", 8:"banana", "a":"apple"}) should return False.
            check_dict_case({"Name":"John", "Age":"36", "City":"Houston"}) should return False.
            check_dict_case({"STATE":"NC", "ZIP":"12345" }) should return True.
            """
        ''',
        '''
        import solution
        assert solution.check_dict_case({"p": "pineapple", "b": "banana"}) is True
        assert solution.check_dict_case({"p": "pineapple", "A": "banana", "B": "banana"}) is False
        assert solution.check_dict_case({"p": "pineapple", 5: "banana", "a": "apple"}) is False
        assert solution.check_dict_case({"Name": "John", "Age": "36", "City": "Houston"}) is False
        assert solution.check_dict_case({"STATE": "NC", "ZIP": "12345"}) is True
        assert solution.check_dict_case({"fruit": "Orange", "taste": "Sweet"}) is True
        assert solution.check_dict_case({}) is False
        print("ok")
        ''',
        "humaneval",
    ),
]

# ---------------------------------------------------------------------------
# original suite — written for this repo (Sept 2026), never published before.
# Deterministic edge cases designed to catch subtle spec violations.
# ---------------------------------------------------------------------------

ORIGINAL = [
    _ft(
        "arc-runlength", "easy", "run_length",
        '''
        def run_length(items):
            """Run-length encode a list.

            Return a list of (value, count) tuples in order, where each maximal
            run of equal adjacent values collapses into one tuple.

            >>> run_length([])
            []
            >>> run_length([7])
            [(7, 1)]
            >>> run_length([4, 4, 2, 2, 2, 4])
            [(4, 2), (2, 3), (4, 1)]
            """
        ''',
        '''
        import solution
        assert solution.run_length([]) == []
        assert solution.run_length([7]) == [(7, 1)]
        assert solution.run_length([4, 4, 2, 2, 2, 4]) == [(4, 2), (2, 3), (4, 1)]
        assert solution.run_length([1, 1, 1, 1]) == [(1, 4)]
        assert solution.run_length(["a", "b", "b", "a", "a", "a"]) == [("a", 1), ("b", 2), ("a", 3)]
        assert solution.run_length([0, 1, 0]) == [(0, 1), (1, 1), (0, 1)]
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-balance-depth", "easy", "balance_depth",
        '''
        def balance_depth(s):
            """Analyze a string of parentheses.

            Return the maximum nesting depth if the string is balanced.
            Return -1 if the string is unbalanced, where unbalanced means:
            a ')' appears with no unmatched '(' before it, or unmatched '('
            remain at the end of the string. Characters other than '(' and ')'
            are ignored but do NOT break nesting.

            >>> balance_depth("")
            0
            >>> balance_depth("(a(b)c)")
            2
            >>> balance_depth(")(")
            -1
            """
        ''',
        '''
        import solution
        assert solution.balance_depth("") == 0
        assert solution.balance_depth("(a(b)c)") == 2
        assert solution.balance_depth(")(") == -1
        assert solution.balance_depth("(()") == -1
        assert solution.balance_depth("())") == -1
        assert solution.balance_depth("x(y(z)w)q") == 2
        assert solution.balance_depth("((()))()") == 3
        assert solution.balance_depth("ab") == 0
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-interleave", "easy", "interleave",
        '''
        def interleave(lists):
            """Round-robin interleave a list of lists.

            Take one element from each sublist in turn, skipping a sublist once
            it is exhausted, until every sublist is exhausted. Return the flat list.

            >>> interleave([[1, 2, 3], ["a", "b"], [True]])
            [1, 'a', True, 2, 'b', 3]
            >>> interleave([])
            []
            """
        ''',
        '''
        import solution
        assert solution.interleave([[1, 2, 3], ["a", "b"], [True]]) == [1, "a", True, 2, "b", 3]
        assert solution.interleave([]) == []
        assert solution.interleave([[], []]) == []
        assert solution.interleave([[1], [2], [3]]) == [1, 2, 3]
        assert solution.interleave([[1, 2], [], [3, 4, 5]]) == [1, 3, 2, 4, 5]
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-window-mode", "medium", "window_mode",
        '''
        def window_mode(nums, k):
            """Sliding-window mode.

            For each contiguous window of length k over nums (windows total:
            len(nums) - k + 1), return the most frequent value in that window.
            Ties are broken by choosing the SMALLER value. Raise ValueError if
            k <= 0 or k > len(nums).

            >>> window_mode([1, 2, 2, 3], 2)
            [1, 2, 2]
            >>> window_mode([5], 1)
            [5]
            """
        ''',
        '''
        import solution
        assert solution.window_mode([1, 2, 2, 3], 2) == [1, 2, 2]
        assert solution.window_mode([5], 1) == [5]
        assert solution.window_mode([1, 1, 2, 2, 1], 3) == [1, 2, 2]
        assert solution.window_mode([3, 3, 1, 1], 2) == [3, 1, 1]
        assert solution.window_mode([9, 8, 7, 8, 9], 3) == [7, 8, 7]
        for bad in (0, -2, 6):
            try:
                solution.window_mode([1, 2, 3, 4, 5], bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"k={bad} did not raise ValueError")
        try:
            solution.window_mode([], 1)
        except ValueError:
            pass
        else:
            raise AssertionError("empty list did not raise ValueError")
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-roman", "medium", "roman_value",
        '''
        def roman_value(s):
            """Convert a strictly valid Roman numeral to its integer value.

            Use standard symbols I=1, V=5, X=10, L=50, C=100, D=500, M=1000 and
            subtractive notation (IV, IX, XL, XC, CD, CM). A numeral is INVALID
            if it violates standard rules: at most three identical additive
            symbols in a row; V, L, D never repeat and never appear
            subtractively; only one smaller symbol may precede a larger one;
            and the only allowed subtractive pairs are I before V/X, X before
            L/C, C before D/M. Also invalid: a smaller symbol after a
            subtractive pair (e.g. "IXI"), wrong-ordering like "IL" or "VX",
            or any symbol outside IVXLCDM. Return -1 for invalid input,
            including the empty string.

            >>> roman_value("MCMXCIV")
            1994
            >>> roman_value("IIII")
            -1
            >>> roman_value("VX")
            -1
            """
        ''',
        '''
        import solution
        assert solution.roman_value("I") == 1
        assert solution.roman_value("III") == 3
        assert solution.roman_value("IV") == 4
        assert solution.roman_value("IX") == 9
        assert solution.roman_value("LVIII") == 58
        assert solution.roman_value("MCMXCIV") == 1994
        assert solution.roman_value("MMCDXXV") == 2425
        assert solution.roman_value("IIII") == -1
        assert solution.roman_value("VV") == -1
        assert solution.roman_value("VX") == -1
        assert solution.roman_value("IL") == -1
        assert solution.roman_value("IXI") == -1
        assert solution.roman_value("XXL") == -1
        assert solution.roman_value("") == -1
        assert solution.roman_value("ABC") == -1
        assert solution.roman_value("iv") == -1
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-weighted-median", "medium", "weighted_median",
        '''
        def weighted_median(pairs):
            """Weighted median of (value, weight) pairs.

            Sort by value. Total weight W is the sum of all weights. The
            weighted median is the smallest value v such that the cumulative
            weight of all values <= v is at least W / 2 (W/2 exactly counts).
            weights are positive ints, values are ints or floats. Raise
            ValueError on an empty list or a non-positive weight.

            >>> weighted_median([(1, 1), (2, 1), (3, 8)])
            3
            >>> weighted_median([(1, 3), (2, 3), (3, 2)])
            2
            """
        ''',
        '''
        import solution
        assert solution.weighted_median([(1, 1), (2, 1), (3, 8)]) == 3
        assert solution.weighted_median([(1, 3), (2, 3), (3, 2)]) == 2
        assert solution.weighted_median([(1, 1), (2, 1)]) == 1
        assert solution.weighted_median([(2.5, 4), (1.5, 1), (9.0, 1)]) == 2.5
        assert solution.weighted_median([(10, 5)]) == 10
        assert solution.weighted_median([(3, 2), (1, 2), (2, 2)]) == 2
        for bad in ([], [(1, 0)], [(1, -2)]):
            try:
                solution.weighted_median(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad} did not raise ValueError")
        print("ok")
        ''',
        "original",
    ),
]

ORIGINAL += [
    _ft(
        "arc-json-merge", "medium", "deep_merge",
        '''
        def deep_merge(a, b):
            """Deep-merge two JSON-like dicts into a NEW dict (inputs unchanged).

            Rules: keys from b override keys from a. If both values are dicts,
            merge them recursively instead. If both values are lists, the result
            is a's list followed by b's list. In every other case b's value wins.
            Do not mutate a or b.

            >>> deep_merge({"a": 1}, {"b": 2})
            {'a': 1, 'b': 2}
            >>> deep_merge({"x": {"y": 1}}, {"x": {"z": 2}})
            {'x': {'y': 1, 'z': 2}}
            """
        ''',
        '''
        import solution
        assert solution.deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
        assert solution.deep_merge({"x": {"y": 1}}, {"x": {"z": 2}}) == {"x": {"y": 1, "z": 2}}
        assert solution.deep_merge({"l": [1]}, {"l": [2, 3]}) == {"l": [1, 2, 3]}
        assert solution.deep_merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}
        assert solution.deep_merge({"a": 5}, {"a": {"b": 1}}) == {"a": {"b": 1}}
        assert solution.deep_merge({}, {"k": [1, 2]}) == {"k": [1, 2]}
        assert solution.deep_merge({"n": {"l": [1], "d": {"p": 0}}}, {"n": {"l": [2], "d": {"q": 1}}}) == \
            {"n": {"l": [1, 2], "d": {"p": 0, "q": 1}}}
        a, b = {"k": {"v": [1]}}, {"k": {"v": [2]}}
        solution.deep_merge(a, b)
        assert a == {"k": {"v": [1]}} and b == {"k": {"v": [2]}}, "inputs must not be mutated"
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-spiral-index", "hard", "spiral_value",
        '''
        def spiral_value(rows, cols, r, c):
            """Value at position (r, c) of a spiral-filled matrix (0-based).

            Imagine a rows x cols matrix filled with the integers 1, 2, 3, ...
            in clockwise order starting at the top-left corner and moving
            right. For example the 3x3 matrix is:
                1 2 3
                8 9 4
                7 6 5
            Return the value at row r, column c (both 0-based). Raise
            ValueError when rows or cols is not positive, or when (r, c) is
            outside the matrix.

            >>> spiral_value(3, 3, 1, 1)
            9
            >>> spiral_value(3, 3, 2, 0)
            7
            """
        ''',
        '''
        import solution
        assert solution.spiral_value(3, 3, 0, 0) == 1
        assert solution.spiral_value(3, 3, 0, 2) == 3
        assert solution.spiral_value(3, 3, 1, 1) == 9
        assert solution.spiral_value(3, 3, 2, 0) == 7
        assert solution.spiral_value(4, 4, 1, 2) == 14
        assert solution.spiral_value(4, 4, 3, 3) == 7
        assert solution.spiral_value(4, 4, 2, 1) == 16
        assert solution.spiral_value(2, 3, 1, 0) == 6
        assert solution.spiral_value(2, 3, 1, 2) == 4
        assert solution.spiral_value(1, 1, 0, 0) == 1
        assert solution.spiral_value(5, 2, 4, 1) == 6
        for bad in ((3, 3, 3, 0), (3, 3, 0, 3), (0, 3, 0, 0), (3, -1, 0, 0)):
            try:
                solution.spiral_value(*bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad} did not raise ValueError")
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-chunk-words", "hard", "chunk_words",
        '''
        def chunk_words(words, limit):
            """Greedy word-wrap into string chunks.

            Pack words (a list of non-empty strings without spaces) into chunks
            in order. A chunk joins words with single spaces and its joined
            length must not exceed limit. Fill each chunk greedily: keep adding
            the next word until it no longer fits, then start a new chunk. A
            single word longer than limit gets its own chunk (it may exceed
            limit). Return the list of chunks. Raise ValueError if limit < 1.

            >>> chunk_words(["a", "bb", "ccc", "dd", "e"], 5)
            ['a bb', 'ccc', 'dd e']
            >>> chunk_words([], 3)
            []
            """
        ''',
        '''
        import solution
        assert solution.chunk_words(["a", "bb", "ccc", "dd", "e"], 5) == ["a bb", "ccc", "dd e"]
        assert solution.chunk_words([], 3) == []
        assert solution.chunk_words(["hello", "supercalifrag", "x"], 4) == ["hello", "supercalifrag", "x"]
        assert solution.chunk_words(["one", "two", "three"], 100) == ["one two three"]
        assert solution.chunk_words(["ab", "cd", "ef", "gh"], 5) == ["ab cd", "ef gh"]
        assert solution.chunk_words(["xyz"], 1) == ["xyz"]
        try:
            solution.chunk_words(["a"], 0)
        except ValueError:
            pass
        else:
            raise AssertionError("limit=0 did not raise ValueError")
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-grid-path", "hard", "min_path_sum",
        '''
        def min_path_sum(grid):
            """Minimum-cost right/down path through a grid with obstacles.

            grid is a list of rows (all rows the same positive length). Cell
            values are ints; -1 marks a blocked cell, all other values are
            travel costs. From the top-left cell you may move only RIGHT or
            DOWN. Return the minimum total cost of a path from (0, 0) to the
            bottom-right cell, counting every cell visited including start and
            end, or -1 if no such path exists (blocked start/end, a dead end
            arrangement, or an empty grid).

            >>> min_path_sum([[1, 3, 1], [1, 5, 1], [4, 2, 1]])
            7
            >>> min_path_sum([[5]])
            5
            """
        ''',
        '''
        import solution
        assert solution.min_path_sum([[1, 3, 1], [1, 5, 1], [4, 2, 1]]) == 7
        assert solution.min_path_sum([[5]]) == 5
        assert solution.min_path_sum([[1, -1, 1]]) == -1
        assert solution.min_path_sum([[1, 2], [-1, 3]]) == 6
        assert solution.min_path_sum([[2, 1, -1], [3, -1, 1], [1, 1, 1]]) == 8
        assert solution.min_path_sum([[-1]]) == -1
        assert solution.min_path_sum([]) == -1
        assert solution.min_path_sum([[0, 0, 0], [0, 0, 0], [0, 0, 0]]) == 0
        assert solution.min_path_sum([[1, 1, 1, 1], [9, 9, 9, 1], [1, 1, 1, 1]]) == 6
        print("ok")
        ''',
        "original",
    ),
    _ft(
        "arc-safe-arith", "hard", "eval_arith",
        '''
        def eval_arith(expr):
            """Evaluate a tiny arithmetic expression, safely.

            Grammar: non-negative integer literals, the binary operators
            + - * // and parentheses; whitespace may appear anywhere. Standard
            precedence applies (* and // bind tighter than + and -) and
            operators of the same precedence associate to the left. '//' is
            Python-style floor division. No unary minus, no other characters.
            Raise ValueError on any syntax that does not fit this grammar and
            on division by zero. (An overflow int will never occur in tests.)

            >>> eval_arith("2+3*4")
            14
            >>> eval_arith("(2+3)*4")
            20
            >>> eval_arith("7//2")
            3
            """
        ''',
        '''
        import solution
        assert solution.eval_arith("2+3*4") == 14
        assert solution.eval_arith("(2+3)*4") == 20
        assert solution.eval_arith("7//2") == 3
        assert solution.eval_arith("10 - 2 - 3") == 5
        assert solution.eval_arith("(1+(2*3))//2") == 3
        assert solution.eval_arith("  42  ") == 42
        assert solution.eval_arith("12 // 5 + 1") == 3
        assert solution.eval_arith("2*(3+(4*5))") == 46
        assert solution.eval_arith("0-7//2") == -3
        for bad in ("-3", "2+", "+", "2..3", "abc", "1//0", "(2", "2 3", "", "3.5+1"):
            try:
                solution.eval_arith(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} did not raise ValueError")
        print("ok")
        ''',
        "original",
    ),
]

# ---------------------------------------------------------------------------
# package suite — multi-file project specs with deterministic pre-written
# tests and fixtures. The model must CREATE src/ files; tests are ground truth.
# ---------------------------------------------------------------------------

def _pt(task_id, tier, prompt, files, timeout=60):
    return {
        "task_id": task_id,
        "suite": "package",
        "tier": tier,
        "kind": "package",
        "entry": "",
        "prompt": _dedent(prompt),
        "files": files,
        "timeout": timeout,
    }


_CSV_FIXTURE = """name,age,score
amy,30,88.5
bob,25,91.0
cy,35,77.25
dan,40,
eve,,92.5
"""

PACKAGE = [
    _pt(
        "pkg-csvstats", "medium",
        '''
        Build a small Python package in the current directory.

        Create EXACTLY these files:

        1. src/csvstats.py — a module with:
           - def column_stats(path, column): read the CSV file at `path`
             (header row present) with the csv module, take the values of
             `column` that parse as float (skip empty strings and
             unparseable cells) and return a dict with EXACTLY the keys
             count, mean, min, max, median (statistics.median for median).
             Raise ValueError if no usable values remain.
           - def summary(path): return a dict mapping every column whose
             name is not the first column to its column_stats dict, skipping
             columns where column_stats raises ValueError.

        2. src/__main__.py — CLI so that `python -m src PATH COLUMN` prints
           one line of JSON: json.dumps(stats, sort_keys=True) where every
           numeric value has been rounded to 4 decimals with round(value, 4)
           FIRST (e.g. {"count": 4, "max": 40, "mean": 32.5, ...}).

        A fixture data.csv and a test file test_package.py already exist.
        Rules: standard library only; do not modify or delete existing files.
        ''',
        {
            "data.csv": _CSV_FIXTURE,
            "test_package.py": _dedent('''
                import json
                import subprocess
                import sys

                from src.csvstats import column_stats, summary

                age = column_stats("data.csv", "age")
                assert age == {"count": 4, "mean": 32.5, "min": 25.0, "max": 40.0, "median": 32.5}, age
                score = column_stats("data.csv", "score")
                assert score["count"] == 4, score
                assert abs(score["mean"] - 87.3125) < 1e-9, score
                assert score["median"] == 89.75, score
                s = summary("data.csv")
                assert set(s) == {"age", "score"}, s
                assert s["age"]["count"] == 4 and s["score"]["max"] == 92.5, s
                try:
                    column_stats("data.csv", "name")
                except ValueError:
                    pass
                else:
                    raise AssertionError("non-numeric column did not raise ValueError")

                out = subprocess.run(
                    [sys.executable, "-m", "src", "data.csv", "age"],
                    capture_output=True, text=True, timeout=30,
                )
                assert out.returncode == 0, out.stderr
                got = json.loads(out.stdout.strip())
                assert got == {"count": 4, "max": 40, "mean": 32.5, "median": 32.5, "min": 25}, got
                print("ok")
            '''),
        },
    ),
    _pt(
        "pkg-jsonql", "medium",
        '''
        Build a small Python package in the current directory.

        Create EXACTLY these files:

        1. src/jsonql.py — a module with:
           - def load(path): parse the JSON file at `path` and return it
             (a list of flat dicts).
           - def select(rows, where=None, fields=None): filter and project
             `rows` (list of dicts). `where` is a dict of field -> condition,
             where a condition is either a plain value (equality match) or a
             dict with any of the operators "$gt", "$lt", "$ne", "$in"
             (list membership). All conditions must hold (AND). A row missing
             a `where` field does NOT match. `fields` is a list of keys to
             keep, in that order; missing keys are omitted. Return the new
             list of dicts, preserving the input row order. Do not mutate
             the input rows.

        2. src/__main__.py — CLI:
             python -m src FILE [--where JSONOBJECT] [--fields a,b,c]
           prints one line per selected row: json.dumps(row, sort_keys=True).

        A fixture people.json and a test file test_package.py already exist.
        Rules: standard library only; do not modify or delete existing files.
        ''',
        {
            "people.json": _dedent('''
                [
                  {"name": "amy", "role": "dev", "age": 30, "city": "raleigh"},
                  {"name": "bob", "role": "pm",  "age": 25, "city": "austin"},
                  {"name": "cy",  "role": "dev", "age": 35, "city": "raleigh"},
                  {"name": "dan", "role": "qa",  "age": 40, "city": "boise"},
                  {"name": "eve", "role": "dev", "age": 28, "city": "austin"}
                ]
            '''),
            "test_package.py": _dedent('''
                import json
                import subprocess
                import sys

                from src.jsonql import load, select

                rows = load("people.json")
                assert len(rows) == 5

                devs = select(rows, {"role": "dev"})
                assert [r["name"] for r in devs] == ["amy", "cy", "eve"]

                older = select(rows, {"age": {"$gt": 28}}, fields=["name", "age"])
                assert older == [
                    {"name": "amy", "age": 30},
                    {"name": "cy", "age": 35},
                    {"name": "dan", "age": 40},
                ], older

                assert select(rows, {"city": "raleigh", "age": {"$lt": 33}}) == [rows[0]]
                assert select(rows, {"role": {"$in": ["pm", "qa"]}, "age": {"$gt": 30}}) == [rows[3]]
                assert select(rows, {"role": {"$ne": "dev"}}) == [rows[1], rows[3]]
                assert select(rows, {"nickname": "x"}) == []
                assert rows[0] == {"name": "amy", "role": "dev", "age": 30, "city": "raleigh"}

                out = subprocess.run(
                    [sys.executable, "-m", "src", "people.json",
                     "--where", '{"role": "dev"}', "--fields", "name"],
                    capture_output=True, text=True, timeout=30,
                )
                assert out.returncode == 0, out.stderr
                assert out.stdout.strip().splitlines() == [
                    '{"name": "amy"}', '{"name": "cy"}', '{"name": "eve"}',
                ], out.stdout
                print("ok")
            '''),
        },
    ),
    _pt(
        "pkg-wordfreq", "medium",
        '''
        Build a small Python package in the current directory.

        Create EXACTLY these files:

        1. src/wordfreq.py — a module with:
           - def tokenize(text): split `text` into lowercase word tokens,
             where a token is a maximal run of ASCII letters and digits
             (a-zA-Z0-9); everything else is a separator. Return the tokens
             lowercased, in order.
           - def top_k(path, k): read the text file at `path`, tokenize it,
             and return the k most frequent tokens as a list of
             (word, count) tuples sorted by count DESCENDING, then word
             ASCENDING. If k exceeds the vocabulary, return all tokens.

        2. src/__main__.py — CLI so that `python -m src PATH K` prints ONE
           line: json.dumps([[word, count], ...]) for top_k(PATH, int(K)).

        A fixture text.txt and a test file test_package.py already exist.
        Rules: standard library only; do not modify or delete existing files.
        ''',
        {
            "text.txt": 'Hello, hello, HELLO! world; world? Data data data. Foo_bar 42 - foo.\n',
            "test_package.py": _dedent('''
                import json
                import subprocess
                import sys

                from src.wordfreq import tokenize, top_k

                assert tokenize("Foo_bar 42 - foo!") == ["foo", "bar", "42", "foo"]
                top3 = top_k("text.txt", 3)
                assert top3 == [("data", 3), ("hello", 3), ("foo", 2)], top3
                top99 = top_k("text.txt", 99)
                assert len(top99) == 6, top99
                assert top99[-2:] == [("42", 1), ("bar", 1)], top99
                assert top99[3][0] == "world", top99

                out = subprocess.run(
                    [sys.executable, "-m", "src", "text.txt", "4"],
                    capture_output=True, text=True, timeout=30,
                )
                assert out.returncode == 0, out.stderr
                assert json.loads(out.stdout.strip()) == [
                    ["data", 3], ["hello", 3], ["foo", 2], ["world", 2],
                ], out.stdout
                print("ok")
            '''),
        },
    ),
]

PACKAGE += [
    _pt(
        "pkg-intervals", "medium",
        '''
        Build a small Python package in the current directory.

        Create EXACTLY this file:

        src/intervals.py — a module with a class IntervalSet of closed
        integer intervals [lo, hi] with:
           - add(lo, hi): insert [lo, hi]; raise ValueError if lo > hi or
             either bound is not an int. Intervals that OVERLAP or TOUCH
             (gap of <= 1, e.g. [1,3] and [4,6]) must merge into one.
           - intervals(): return the stored intervals as a list of
             [lo, hi] lists, sorted and merged.
           - contains(x): True if x lies inside some stored interval.
           - complement(lo, hi): return the gaps within [lo, hi] not covered
             by any stored interval, as a sorted list of [lo, hi] lists
             (clipped to [lo, hi]); raise ValueError if lo > hi.

        A test file test_package.py already exists.
        Rules: standard library only; do not modify or delete existing files.
        ''',
        {
            "test_package.py": _dedent('''
                from src.intervals import IntervalSet

                s = IntervalSet()
                s.add(1, 3)
                s.add(5, 7)
                s.add(2, 6)
                assert s.intervals() == [[1, 7]], s.intervals()
                assert s.contains(4) is True
                assert s.contains(9) is False
                assert s.complement(0, 10) == [[0, 0], [8, 10]]

                s2 = IntervalSet()
                s2.add(3, 4)
                s2.add(10, 12)
                s2.add(0, 1)
                assert s2.intervals() == [[0, 1], [3, 4], [10, 12]], s2.intervals()
                s2.add(1, 3)
                assert s2.intervals() == [[0, 4], [10, 12]], s2.intervals()
                assert s2.intervals()[0] == [0, 4]

                s3 = IntervalSet()
                assert s3.intervals() == []
                assert s3.contains(0) is False
                assert s3.complement(2, 5) == [[2, 5]]

                s4 = IntervalSet()
                s4.add(5, 5)
                s4.add(1, 2)
                s4.add(4, 4)
                assert s4.intervals() == [[1, 2], [4, 5]], s4.intervals()
                assert s4.complement(0, 6) == [[0, 0], [3, 3], [6, 6]]

                for bad in ((4, 2), (1.5, 3), ("a", 5)):
                    try:
                        IntervalSet().add(*bad)
                    except (ValueError, TypeError):
                        pass
                    else:
                        raise AssertionError(f"add{bad} did not raise")
                try:
                    IntervalSet().complement(9, 1)
                except ValueError:
                    pass
                else:
                    raise AssertionError("inverted complement did not raise")
                print("ok")
            '''),
        },
    ),
    _pt(
        "pkg-cronish", "hard",
        '''
        Build a small Python package in the current directory.

        Create EXACTLY these files:

        1. src/fields.py — def parse_field(spec, lo, hi): parse one cron
           field into a sorted list of ints within [lo, hi] inclusive.
           Support "*" (all values), "*/k" (every k starting at lo),
           "a" (single), "a-b" (inclusive range), "a-b/k" (every k within
           the range), and comma-separated combinations of those. Raise
           ValueError for out-of-range values, k <= 0, empty parts, or
           non-numeric tokens.

        2. src/nextrun.py — def next_run(spec, after): `spec` is a 5-field
           cron string "minute hour day-of-month month day-of-week" (dow:
           classic cron numbering: 0 or 7 = Sunday, 1 = Monday ..
           6 = Saturday) and `after` is a datetime.datetime.
           Return the first datetime strictly AFTER `after` whose fields
           all match, at second=0 and microsecond=0. Standard cron rule:
           if BOTH day-of-month and day-of-week are restricted (neither is
           "*"), a date matches when EITHER field matches; otherwise both
           must match. Return a datetime (same naive/aware state as input).
           Correct across month/year boundaries. Raise ValueError on bad
           field specs or a spec whose times can never occur (e.g.
           Feb 30 with dow="*").

        A test file test_package.py already exists.
        Rules: standard library only; do not modify or delete existing files.
        ''',
        {
            "test_package.py": _dedent('''
                from datetime import datetime

                from src.fields import parse_field
                from src.nextrun import next_run

                assert parse_field("*", 0, 59) == list(range(60))
                assert parse_field("*/15", 0, 59) == [0, 15, 30, 45]
                assert parse_field("5", 0, 59) == [5]
                assert parse_field("1-4", 0, 6) == [1, 2, 3, 4]
                assert parse_field("0-10/3", 0, 59) == [0, 3, 6, 9]
                assert parse_field("1,5,9", 0, 59) == [1, 5, 9]
                for bad in ("60", "-1", "a", "*/0", "", "1,,2"):
                    try:
                        parse_field(bad, 0, 59)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError(f"parse_field({bad!r}) did not raise")

                assert next_run("0 9 * * *", datetime(2026, 9, 9, 8, 0)) == datetime(2026, 9, 9, 9, 0)
                assert next_run("0 9 * * *", datetime(2026, 9, 9, 9, 0)) == datetime(2026, 9, 10, 9, 0)
                assert next_run("*/15 * * * *", datetime(2026, 9, 9, 9, 3)) == datetime(2026, 9, 9, 9, 15)
                assert next_run("30 14 28 2 *", datetime(2026, 9, 9, 0, 0)) == datetime(2027, 2, 28, 14, 30)
                assert next_run("0 0 * * 1", datetime(2026, 9, 9, 12, 0)) == datetime(2026, 9, 14, 0, 0)
                assert next_run("0 0 13 * 5", datetime(2026, 9, 9, 0, 0)) == datetime(2026, 9, 11, 0, 0)
                assert next_run("0 0 31 12 *", datetime(2026, 6, 1, 0, 0)) == datetime(2026, 12, 31, 0, 0)
                assert next_run("45 23 31 1 *", datetime(2026, 1, 31, 23, 45)) == datetime(2027, 1, 31, 23, 45)
                try:
                    next_run("0 0 30 2 *", datetime(2026, 1, 1))
                except ValueError:
                    pass
                else:
                    raise AssertionError("Feb 30 did not raise ValueError")
                print("ok")
            '''),
        },
    ),
]

# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

SUITES = {
    "humaneval": {
        "desc": "canonical HumanEval-style function tasks (subset; saturated for frontier models, use as sanity baseline)",
        "tasks": HUMANEVAL,
    },
    "original": {
        "desc": "hand-written 2026 function tasks with deterministic edge cases (contamination-resistant)",
        "tasks": ORIGINAL,
    },
    "package": {
        "desc": "multi-file mini-project specs with deterministic tests (suited to CLI harnesses and the multi-block direct harness)",
        "tasks": PACKAGE,
    },
}


def tasks_for(suites, tiers=None, limit=None):
    """Tasks for the comma-joined suite names, optionally tier-filtered."""
    out = []
    for name in suites:
        if name not in SUITES:
            raise ValueError(f"unknown suite {name!r}; choices: {sorted(SUITES)}")
        out.extend(SUITES[name]["tasks"])
    if tiers:
        out = [t for t in out if t["tier"] in tiers]
    if limit:
        out = out[:limit]
    return out
