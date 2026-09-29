#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

The whole CLI: turn argv and stdin into one dic() call, and leave without
waiting for the interpreter to tear down.  Everything else is a library, so
the only work left here is the work a library must not do: read the process's
stdin, and turn a DicError or a ^C into an exit status.

    dic/dic.py          arguments, stdin, os._exit
    dic/client.py       dic(), Reply -- the program
    dic/options.py      Flag: one declaration per knob, the CLI, the env vars
    dic/config.py       model and provider config: json sources, sqlite cache
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/tty.py          colour, errors, the cost line, the progress meters
    dic/tool.py         --tools: python functions the model may call
    dic/tools/*.py      the tools that ship with dic, one category each
    dic/adaptors/*.py   one wire protocol each
    dic/models.json     packaged defaults, overlaid by the user's files
"""
import time                     # first, so that T0 measures dic's own startup
T0 = time.time_ns()             # cost -- imports, config, db -- as well as the API's

import os, sys

from dic.client import dic
from dic.options import parser
from dic.tty import DicError, die


def load_adaptor(api_type):
    """The module implementing api_type: PATH, auth, build, parse, finish.

    Built-ins are dic.adaptors.<api_type with '-' as '_'>.  Anything else
    must be ~/.config/fac/adapters/<api_type>.py exporting the same five
    names and importing dic's own helpers by package name, e.g.
    `from dic.store import data_url`.  Only the selected adaptor is ever
    imported, and each one gets its own module name, so two of them cannot
    collide.
    """
    import importlib
    name = api_type.replace("-", "_")
    if os.path.exists(os.path.join(HERE, "adaptors", f"{name}.py")):
        return importlib.import_module(f"dic.adaptors.{name}")
    path = os.path.join(config_dir(env), "adapters", f"{api_type}.py")
    if not os.path.exists(path):
        die(f"unknown api_type: {api_type}")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"dic_adaptor_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    try:
        parsed, extra = parser().parse_known_args()
        args = vars(parsed)
        # Every word argparse did not claim is a prompt word too.  A wrapper
        # such as committe puts its own instructions in front of the request
        # it forwards, and argparse matches only one run of positionals, so
        # the words on either side of a flag would otherwise be dropped.
        prompt = " ".join(args.pop("prompt") + extra)
        if not sys.stdin.isatty():
            piped = sys.stdin.read()
            prompt = f"{prompt}\n\n{piped}" if (prompt and piped.strip()) else (prompt or piped)
        dic(prompt, **args, t_start=T0, env=os.environ, out=sys.stdout, err=sys.stderr)
        sys.stdout.flush()
    except DicError as e:
        # dic() reports a failure by raising, so the CLI is the one place
        # that decides its colour, its stream and its exit code
        die(e, err=sys.stderr, env=os.environ)
    except KeyboardInterrupt:
        # dic() lets a ^C during a reply propagate too: cancelled is a failure
        # like any other, and the exit code is the CLI's business
        die("cancelled", err=sys.stderr, env=os.environ)
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
