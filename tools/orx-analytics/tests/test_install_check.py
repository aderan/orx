"""Installation validation distinguishes path aliases from source leakage."""

import pytest

from scripts.check_install import require_installed_module


def test_installed_module_accepts_environment_symlink_alias(tmp_path):
    environment = tmp_path / "environment"
    module = environment / "lib" / "orxanalytics" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.touch()
    alias = tmp_path / "environment-alias"
    alias.symlink_to(environment, target_is_directory=True)

    require_installed_module(module, alias)


def test_installed_module_rejects_checkout_outside_environment(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    checkout = tmp_path / "checkout" / "orxanalytics" / "__init__.py"
    checkout.parent.mkdir(parents=True)
    checkout.touch()

    with pytest.raises(RuntimeError, match="outside its installed environment"):
        require_installed_module(checkout, environment)
