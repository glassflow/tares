Fixed
The merge queue could merge a pull request whose `factory/review` status did not pass on the commit made by its branch update, when that status was the only review (TR-1663). It now leaves the pull request out of the queue until the review passes.
