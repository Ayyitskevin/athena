"""Architecture imports must keep web over independent domains over core."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_import_contracts.py"


def _package(tmp_path: Path, modules: dict[str, str]) -> Path:
    package_root = tmp_path / "src" / "athena"
    packages = (
        package_root,
        *(package_root / area for area in ("core", "aegis", "mentor", "web")),
    )
    for package in packages:
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "main.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
        path = package_root.joinpath(*module.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return package_root


def _run(
    package_root: Path | None = None,
    *,
    hash_seed: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT)]
    if package_root is not None:
        command.extend(("--package-root", str(package_root)))
    env = None
    if hash_seed is not None:
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_current_source_obeys_import_contracts():
    result = _run()

    assert result.returncode == 0, result.stderr
    assert "no forbidden dependencies" in result.stdout


def test_allowed_layer_direction_passes(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.foundation": "VALUE = 1\n",
            "aegis.work": "from athena.core import foundation\n",
            "mentor.docs": "from athena.core import foundation\n",
            "web.views": (
                "from athena.aegis import work\n"
                "from athena.core import foundation\n"
                "from athena.mentor import docs\n"
            ),
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("module", "source", "message"),
    [
        (
            "core.leak",
            "from athena.web import router\n",
            "core cannot depend on web",
        ),
        (
            "core.leak",
            "import athena.aegis\n",
            "core cannot depend on aegis",
        ),
        (
            "core.leak",
            "import athena.mentor\n",
            "core cannot depend on mentor",
        ),
        (
            "aegis.leak",
            "from athena.mentor import pages\n",
            "aegis cannot depend on mentor",
        ),
        (
            "aegis.leak",
            "import athena.web\n",
            "aegis cannot depend on web",
        ),
        (
            "mentor.leak",
            "from ..aegis import issues\n",
            "mentor cannot depend on aegis",
        ),
        (
            "mentor.leak",
            "import athena.web\n",
            "mentor cannot depend on web",
        ),
        (
            "core.leak",
            "from athena import web\n",
            "core cannot depend on web",
        ),
        (
            "core.cycle",
            "import athena.main\n",
            "core cannot depend on main",
        ),
        (
            "aegis.cycle",
            "import athena.main\n",
            "aegis cannot depend on main",
        ),
        (
            "mentor.cycle",
            "import athena.main\n",
            "mentor cannot depend on main",
        ),
        (
            "web.cycle",
            "import athena.main\n",
            "web cannot depend on main",
        ),
        (
            "core.cycle",
            'import importlib\nimportlib.import_module("athena.main")\n',
            "core cannot depend on main",
        ),
        (
            "web.cycle",
            '__import__("athena.main")\n',
            "web cannot depend on main",
        ),
    ],
)
def test_forbidden_direct_and_relative_imports_fail(tmp_path, module, source, message):
    package_root = _package(tmp_path, {module: source})

    result = _run(package_root)

    assert result.returncode == 1
    assert message in result.stderr
    assert f"athena.{module}" in result.stderr


def test_indirect_import_through_uncontracted_module_fails(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "bridge": "from athena.web import router\n",
            "core.leak": "from athena import bridge\n",
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "core cannot depend on web" in result.stderr
    assert "athena.core.leak -> athena.bridge -> athena.web" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module(name='athena.web')\n",
        "import importlib\nimportlib.import_module('.web', package='athena')\n",
        "__import__('athena.web')\n",
        "__import__('web', {'__package__': 'athena'}, {}, (), 1)\n",
        (
            "__import__(name='web', globals={'__package__': 'athena.core'}, "
            "locals={}, fromlist=(), level=2)\n"
        ),
        "__import__('athena', fromlist=('web',))\n",
        ("__import__('', {'__package__': 'athena'}, {}, ('web',), 1)\n"),
    ],
)
def test_supported_literal_dynamic_imports_respect_boundaries(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert "core cannot depend on web" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module('athena.core')\n",
        "import importlib\nimportlib.import_module('.core', package='athena')\n",
        "__import__('athena.core')\n",
    ],
)
def test_supported_literal_dynamic_imports_allow_outer_to_inner(tmp_path, source):
    package_root = _package(tmp_path, {"web.views": source})

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module('json')\n",
        "__import__('json')\n",
    ],
)
def test_literal_external_dynamic_imports_remain_outside_contract(tmp_path, source):
    package_root = _package(tmp_path, {"core.clean": source})

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import importlib as loader\n",
        "import importlib.util\n",
        "import importlib.metadata as metadata\n",
        "import importlib.__init__ as loader\n",
        "from importlib import import_module\n",
        "from importlib.metadata import import_module\n",
        "from importlib.__init__ import import_module\n",
        "from importlib.__init__ import *\n",
        "import builtins\n",
        "import builtins as tools\n",
        "from builtins import getattr as lookup\n",
    ],
)
def test_unsupported_import_machinery_syntax_fails_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.clean": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert "only a direct top-level import importlib is supported" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "__import__('importlib')\n",
        "__import__('builtins')\n",
        (
            "__import__('importlib', fromlist=('metadata',))"
            ".metadata.import_module('athena.web')\n"
        ),
        ("getattr(__import__('importlib'), 'import_module')('athena.web')\n"),
        (
            "import importlib\n"
            "importlib.import_module('.', 'importlib')"
            ".import_module('athena.web')\n"
        ),
        (
            "import importlib\n"
            "getattr(importlib.import_module('.', 'importlib'), "
            "'import_module')('athena.web')\n"
        ),
    ],
)
def test_dynamic_import_machinery_reacquisition_fails_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import of machinery module" in result.stderr


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            'import pkgutil\npkgutil.importlib.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "import pkgutil\n"
            "machinery = pkgutil.importlib\n"
            'machinery.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'from pkgutil import importlib\nimportlib.import_module("athena.web")\n',
            "re-exported dynamic import machinery is not supported",
        ),
        (
            "from pkgutil import importlib as machinery\n"
            'machinery.import_module("athena.web")\n',
            "re-exported dynamic import machinery is not supported",
        ),
        (
            'import zipfile\nzipfile.importlib.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import sys\nsys.modules["importlib"].import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            '__import__("pkgutil").importlib.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'globals()["__builtins__"]["__import__"]("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'getattr(__builtins__, "__import__")("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "__builtins__ = {}\n",
            "shadowing the reserved built-in name __builtins__",
        ),
        (
            'import sys\nsys.modules["builtins"].__import__("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'from modulefinder import *\nimportlib.import_module("athena.web")\n',
            "external wildcard import is not supported",
        ),
        (
            'import sys\nsys.modules.get("importlib").import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'globals().get("__builtins__")["__import__"]("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "from modulefinder import __builtins__ as machinery\n"
            'machinery["__import__"]("athena.web")\n',
            "re-exported dynamic import machinery is not supported",
        ),
        (
            'from _frozen_importlib import __import__ as load\nload("athena.web")\n',
            "only a direct top-level import importlib is supported",
        ),
        (
            "import _frozen_importlib as machinery\n"
            'machinery.__import__("athena.web")\n',
            "only a direct top-level import importlib is supported",
        ),
        (
            '__import__("_frozen_importlib").__import__("athena.web")\n',
            "dynamic import of machinery module _frozen_importlib",
        ),
        (
            'from pydantic import import_module as load\nload("athena.web")\n',
            "re-exported dynamic import machinery is not supported",
        ),
        (
            'import pydantic\npydantic.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import pydantic\ngetattr(pydantic, "import_module")("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "import anyio._core._eventloop\n"
            'anyio._core._eventloop.import_module("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import carrier\ncarrier.__dict__["import_module"]("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import carrier\ncarrier.__dict__.get("import_module")("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import carrier\ncarrier.__dict__["__import__"]("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            'import carrier\ncarrier.__dict__.get("__import__")("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "import sys\n"
            'machinery = sys.modules["importlib._bootstrap"]\n'
            'machinery.__dict__["__import__"]("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
        (
            "import sys\n"
            'machinery = sys.modules.get("importlib._bootstrap")\n'
            'machinery.__dict__.get("__import__")("athena.web")\n',
            "unsupported dynamic import machinery expression",
        ),
    ],
)
def test_explicit_machinery_reexports_and_reflection_fail_closed(
    tmp_path, source, message
):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        ("import importlib\ntarget = 'athena.web'\nimportlib.import_module(target)\n"),
        "target = 'athena.web'\n__import__(target)\n",
    ],
)
def test_computed_dynamic_import_fails_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import target must be a string literal" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        ("import importlib\nloader = importlib\nloader.import_module('athena.web')\n"),
        ("loader = __import__('importlib')\nloader.import_module('athena.web')\n"),
        (
            "target = 'athena.web'\n"
            "__import__('importlib').__dict__['import_module'](target)\n"
        ),
        (
            "target = 'athena.web'\n"
            "__import__('importlib').__getattribute__('import_module')(target)\n"
        ),
        (
            "target = 'athena.web'\n"
            "getattr(__import__('importlib'), '__dict__')['import_module'](target)\n"
        ),
        ("import importlib\nload = importlib.import_module\nload('athena.web')\n"),
        "load = __import__\nload('athena.web')\n",
        (
            "import importlib\n"
            "load = getattr(importlib, 'import_module')\n"
            "load('athena.web')\n"
        ),
        (
            "import importlib\n"
            "load, marker = importlib.import_module, object()\n"
            "load('athena.web')\n"
        ),
        (
            "import importlib\n"
            "def run(load=importlib.import_module):\n"
            "    return load('athena.web')\n"
        ),
        ("import importlib\n(load := importlib.import_module)('athena.web')\n"),
        (
            "import importlib\n"
            "load = importlib.import_module\n"
            "accept = lambda value: value\n"
            "accept(load)('athena.web')\n"
        ),
        (
            "import importlib\n"
            "class Box:\n"
            "    pass\n"
            "box = Box()\n"
            "load = box.run = importlib.import_module\n"
            "box.run('athena.web')\n"
        ),
        (
            "import importlib\n"
            "lookup, marker = getattr, object()\n"
            "lookup(importlib, 'import_module')('athena.web')\n"
        ),
        (
            "import importlib\n"
            "class Loaders:\n"
            "    load = importlib.import_module\n"
            "Loaders.load('athena.web')\n"
        ),
    ],
)
def test_dynamic_import_loader_escapes_fail_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert (
        "dynamic import loader must be called directly" in result.stderr
        or "dynamic import module importlib must be used directly" in result.stderr
        or "dynamic import of machinery module importlib" in result.stderr
    )


def test_dynamic_loader_assignment_fails_before_unrelated_scope_use(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "import importlib\n\n"
                "def capture() -> object:\n"
                "    loader = importlib.import_module\n"
                "    return None\n\n"
                "def transform() -> str:\n"
                "    loader = lambda value: value\n"
                "    return loader('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import loader must be called directly" in result.stderr
    assert "core cannot depend on web" not in result.stderr


def test_shadowed_dynamic_import_alias_fails_without_false_boundary(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "import importlib\n"
                "from pathlib import Path as importlib\n"
                "importlib('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "re-exported dynamic import machinery is not supported" in result.stderr
    assert "core cannot depend on web" not in result.stderr


def test_dynamic_import_wildcard_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.leak": ("from importlib import *\nimport_module('athena.web')\n")},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "only a direct top-level import importlib is supported" in result.stderr


def test_external_wildcard_import_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.clean": ("from math import *\nVALUE = pi\n")},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "external wildcard import is not supported" in result.stderr


@pytest.mark.parametrize("name", ["__import__", "getattr"])
def test_pattern_binding_reserved_builtin_name_fails_loudly(tmp_path, name):
    package_root = _package(
        tmp_path,
        {"core.clean": f"match object():\n    case {name}:\n        pass\n"},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert f"shadowing the reserved built-in name {name}" in result.stderr


def test_shadowing_builtin_import_name_fails_loudly(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "def __import__(name: str) -> str:\n"
                "    return name\n\n"
                "VALUE = __import__('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "shadowing the reserved built-in name __import__" in result.stderr
    assert "core cannot depend on web" not in result.stderr


def test_shadowing_getattr_name_fails_loudly(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "def getattr(value: object, name: str) -> str:\n    return name\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "shadowing the reserved built-in name getattr" in result.stderr


def test_opaque_dynamic_import_loader_attribute_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.leak": (
                "import importlib\n"
                "attribute = 'import_module'\n"
                "getattr(importlib, attribute)('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import module importlib must be used directly" in result.stderr


def test_opaque_dynamic_import_fromlist_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.leak": (
                "children = ('web',)\n__import__('athena', fromlist=children)\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import fromlist must be a literal string collection" in (
        result.stderr
    )


@pytest.mark.parametrize(
    "source",
    [
        "__import__('athena', **{'fromlist': ('web',)})\n",
        "arguments = ('athena.web',)\n__import__(*arguments)\n",
    ],
)
def test_dynamic_import_argument_expansions_fail_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import arguments must not use starred expansion" in result.stderr


def test_dynamic_import_module_container_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.leak": ("import importlib\narguments = (importlib, 'import_module')\n")},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import module importlib must be used directly" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        (
            "class Loaders:\n"
            "    import importlib\n"
            "Loaders.importlib.import_module('athena.web')\n"
        ),
        (
            "def load() -> object:\n"
            "    import importlib\n"
            "    return importlib.import_module('athena.web')\n"
        ),
        ("if True:\n    import importlib\n    importlib.import_module('athena.web')\n"),
        ("try:\n    import importlib\nexcept ImportError:\n    pass\n"),
    ],
)
def test_nested_import_machinery_fails_closed(tmp_path, source):
    package_root = _package(tmp_path, {"core.leak": source})

    result = _run(package_root)

    assert result.returncode == 1
    assert (
        "dynamic import machinery must use a direct top-level import" in result.stderr
    )


def test_internal_wildcard_import_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.leak": "from athena import *\n"},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "internal wildcard import is not supported" in result.stderr


def test_computed_dynamic_import_level_fails_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.leak": (
                "level = 2\n"
                "__import__('web', {'__package__': 'athena.core'}, "
                "{}, (), level)\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "dynamic import level must be an integer literal" in result.stderr


def test_opaque_relative_dynamic_import_globals_fail_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.leak": ("__import__('web', globals(), locals(), (), 2)\n")},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "relative dynamic import globals must be a literal string-keyed" in (
        result.stderr
    )


def test_computed_relative_dynamic_import_globals_fail_closed(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.leak": (
                "key = '__package__'\n"
                "__import__('web', {'__package__': 'athena.core', "
                "key: 'athena'}, {}, (), 1)\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "relative dynamic import globals must be a literal string-keyed" in (
        result.stderr
    )


def test_parent_package_initialization_is_part_of_graph(tmp_path):
    package_root = _package(tmp_path, {"core.clean": "VALUE = 1\n"})
    (package_root / "__init__.py").write_text(
        "import athena.web\n",
        encoding="utf-8",
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "core cannot depend on web" in result.stderr
    assert "athena.core -> athena -> athena.web" in result.stderr


def test_unimported_parameter_named_importlib_is_not_dynamic_import(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "class Client:\n"
                "    def import_module(self, name: str) -> str:\n"
                "        return name\n\n"
                "def render(importlib: Client) -> str:\n"
                "    return importlib.import_module('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_unimported_object_getattr_named_import_module_is_not_dynamic_import(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "class Client:\n"
                "    def import_module(self, name: str) -> str:\n"
                "        return name\n\n"
                "client = Client()\n"
                "VALUE = getattr(client, 'import_module')('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_unimported_object_loader_mapping_is_not_dynamic_import(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                "class Client:\n"
                "    pass\n\n"
                "client = Client()\n"
                "client.import_module = lambda name: name\n"
                "VALUE = client.__dict__['import_module']('athena.web')\n"
                "OTHER = client.__dict__.get('import_module')('athena.web')\n"
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_unrelated_import_module_mapping_key_is_not_machinery(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.clean": (
                'mapping = {"import_module": lambda value: value}\n'
                'VALUE = mapping["import_module"]("athena.web")\n'
            )
        },
    )

    result = _run(package_root)

    assert result.returncode == 0, result.stderr


def test_symlinked_source_path_fails_loudly(tmp_path):
    package_root = _package(tmp_path, {"core.clean": "VALUE = 1\n"})
    target = tmp_path / "bridge-target"
    target.mkdir()
    (target / "__init__.py").write_text(
        "import athena.web\n",
        encoding="utf-8",
    )
    (package_root / "bridge").symlink_to(target, target_is_directory=True)

    result = _run(package_root)

    assert result.returncode == 1
    assert "symlinked source paths are not supported" in result.stderr


def test_symlinked_package_root_fails_loudly(tmp_path):
    package_root = _package(tmp_path, {"core.clean": "VALUE = 1\n"})
    link_parent = tmp_path / "linked"
    link_parent.mkdir()
    linked_package_root = link_parent / "athena"
    linked_package_root.symlink_to(package_root, target_is_directory=True)

    result = _run(linked_package_root)

    assert result.returncode == 1
    assert "symlinked package root is not supported" in result.stderr


def test_nested_type_checking_import_fails(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.leak": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    import athena.web.router\n"
            ),
            "web.router": "",
        },
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "core cannot depend on web" in result.stderr


def test_duplicate_module_identity_fails_loudly(tmp_path):
    package_root = _package(tmp_path, {"bridge": "VALUE = 1\n"})
    duplicate_package = package_root / "bridge"
    duplicate_package.mkdir()
    (duplicate_package / "__init__.py").write_text(
        "import athena.web\n",
        encoding="utf-8",
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "duplicate module identity athena.bridge" in result.stderr


def test_diagnostics_are_deterministic(tmp_path):
    package_root = _package(
        tmp_path,
        {
            "core.zeta": "import athena.web\n",
            "core.alpha": "from athena import mentor\n",
        },
    )
    expected = (
        "Athena import contracts failed: 2 forbidden dependency paths\n"
        "src/athena/core/alpha.py:1: core cannot depend on mentor\n"
        "  import chain: athena.core.alpha -> athena.mentor\n"
        "  statement: from athena import mentor\n"
        "src/athena/core/zeta.py:1: core cannot depend on web\n"
        "  import chain: athena.core.zeta -> athena.web\n"
        "  statement: import athena.web\n"
    )

    first = _run(package_root, hash_seed="1")
    second = _run(package_root, hash_seed="999")

    assert first.returncode == second.returncode == 1
    assert first.stderr == second.stderr == expected


def test_malformed_python_fails_loudly(tmp_path):
    package_root = _package(tmp_path, {"core.broken": "def broken(:\n"})

    result = _run(package_root)

    assert result.returncode == 1
    assert "cannot parse" in result.stderr


def test_unresolvable_relative_import_fails_loudly(tmp_path):
    package_root = _package(
        tmp_path,
        {"core.broken": "from ....web import router\n"},
    )

    result = _run(package_root)

    assert result.returncode == 1
    assert "cannot resolve relative import" in result.stderr
