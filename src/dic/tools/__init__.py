"""The tools that ship with dic: one module per category.

`--tools dic.tools.fs:*` offers a whole category and `--tools
dic.tools.fs:ls` offers one tool of it.  A category is a module because a
module is what `*` means, and it is imported only when a --tools value names
it, so an invocation that offers no tools pays for none of them.
"""
