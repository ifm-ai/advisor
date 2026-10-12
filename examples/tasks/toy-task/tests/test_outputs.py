import pytest
from stats import median


def test_odd_length():
    assert median([3, 1, 2]) == 2


def test_even_length_averages_the_middle_pair():
    assert median([4, 1, 3, 2]) == 2.5


def test_empty_list_raises_value_error():
    with pytest.raises(ValueError):
        median([])
