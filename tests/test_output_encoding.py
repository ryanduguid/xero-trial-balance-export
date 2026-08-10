"""Every string these three modules can print must be encodable anywhere.

export_tb.main() reconfigures stdout and stderr to UTF-8 before anything is
printed, because an org name is remote input and can hold any character.
auth.py has no equivalent and needs none: the only remote value it prints is
an OAuth error code it has already matched against ERROR_CODE. What both
entry points do share is their own message literals, and those went out to
whatever encoding the stream happened to have. An em dash in one of them is
fine on a console and fine on a cp1252 box - the stated target - and raises
UnicodeEncodeError on cp437, cp850 or cp932 with output redirected to a file
or a Task Scheduler log, which replaces the instruction the operator needs
with an encoding error about the instruction.

So the rule is that no string literal in the shipped modules is non-ASCII.
Prose is exempt: a comment is not a string once the file is parsed, and a
docstring is read in an editor rather than printed. Test fixtures are exempt
too - they carry deliberately non-ASCII org names, which is the case the
reconfigure exists for.
"""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ("export_tb.py", "xero_client.py", "auth.py")


def non_ascii_literals(source, filename="<test>"):
    """Every non-ASCII string constant in source that is not a docstring."""
    tree = ast.parse(source, filename=filename)
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    documentation = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None) if isinstance(node, holders) else None
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            documentation.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
        and not node.value.isascii()
    ]


class RuntimeOutputIsAsciiTest(unittest.TestCase):
    def test_no_shipped_module_holds_a_non_ascii_string_literal(self):
        for module in MODULES:
            with open(os.path.join(ROOT, module), encoding="utf-8") as handle:
                source = handle.read()
            offenders = non_ascii_literals(source, module)
            with self.subTest(module=module):
                self.assertEqual(
                    [(line, ascii(value)) for line, value in offenders],
                    [],
                    f"{module} holds a literal that cannot be printed to a "
                    "stream on a legacy Windows codepage",
                )

    def test_the_scan_reads_prose_and_output_apart(self):
        """The guard above is worth nothing if the walk misses the literals."""
        self.assertEqual(non_ascii_literals('"""Doc — dash."""\nx = "plain"\n'), [])
        self.assertEqual(
            non_ascii_literals('"""Doc."""\nprint("a — b")\n'),
            [(2, "a — b")],
        )


if __name__ == "__main__":
    unittest.main()
