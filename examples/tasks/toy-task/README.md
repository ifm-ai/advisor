# toy-task: private notes

The visible symptom is the even-length case: `median([4, 1, 3, 2])` returns 3 instead of 2.5.
The second requirement is the one solvers miss: `median([])` must raise `ValueError`, while the
starting code raises `IndexError`. The hidden tests check the odd-length case, the even-length
case, and the empty list.
