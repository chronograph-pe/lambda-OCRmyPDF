# © 2016 James R. Barlow: github.com/jbarlow83
#
# This file is part of OCRmyPDF.
#
# OCRmyPDF is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OCRmyPDF is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OCRmyPDF.  If not, see <http://www.gnu.org/licenses/>.

import logging
import multiprocessing
import os
import warnings
from collections.abc import Iterable
from contextlib import suppress
from functools import wraps
from pathlib import Path

log = logging.getLogger(__name__)


def re_symlink(input_file, soft_link_name, *args, **kwargs):
    """
    Helper function: relinks soft symbolic link if necessary
    """
    if len(args) == 1 and isinstance(args[0], logging.Logger):
        log.warning("Deprecated: re_symlink(,log)")
    if 'log' in kwargs:
        log.warning('Deprecated: re_symlink(...log=)')

    input_file = os.fspath(input_file)
    soft_link_name = os.fspath(soft_link_name)

    # Guard against soft linking to oneself
    if input_file == soft_link_name:
        log.warning(
            "No symbolic link made. You are using "
            "the original data directory as the working directory."
        )
        return

    # Soft link already exists: delete for relink?
    if os.path.lexists(soft_link_name):
        # do not delete or overwrite real (non-soft link) file
        if not os.path.islink(soft_link_name):
            raise FileExistsError(f"{soft_link_name} exists and is not a link")
        try:
            os.unlink(soft_link_name)
        except OSError:
            log.debug("Can't unlink %s", soft_link_name)

    if not os.path.exists(input_file):
        raise FileNotFoundError(f"trying to create a broken symlink to {input_file}")

    log.debug("os.symlink(%s, %s)", input_file, soft_link_name)

    # Create symbolic link using absolute path
    os.symlink(os.path.abspath(input_file), soft_link_name)


def is_iterable_notstr(thing):
    return isinstance(thing, Iterable) and not isinstance(thing, str)


def monotonic(L):
    """Does list increase monotonically?"""
    return all(b > a for a, b in zip(L, L[1:]))


def page_number(input_file):
    """Get one-based page number implied by filename (000002.pdf -> 2)"""
    return int(os.path.basename(os.fspath(input_file))[0:6])


def available_cpu_count():
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        pass
    warnings.warn(
        "Could not get CPU count.  Assuming one (1) CPU." "Use -j N to set manually."
    )
    return 1


def is_file_writable(test_file):
    """Intentionally racy test if target is writable.

    We intend to write to the output file if and only if we succeed and
    can replace it atomically. Before doing the OCR work, make sure
    the location is writable.
    """
    p = Path(test_file)

    if p.is_symlink():
        p = p.resolve(strict=False)

    # p.is_file() throws an exception in some cases
    if p.exists() and p.is_file():
        return os.access(
            os.fspath(p),
            os.W_OK,
            effective_ids=(os.access in os.supports_effective_ids),
        )
    else:
        try:
            fp = p.open('wb')
        except OSError:
            return False
        else:
            fp.close()
            with suppress(OSError):
                p.unlink()
        return True


def deprecated(func):
    """Warn that function is deprecated"""

    @wraps(func)
    def new_func(*args, **kwargs):
        warnings.simplefilter('always', DeprecationWarning)  # turn off filter
        warnings.warn(
            "Call to deprecated function {}.".format(func.__name__),
            category=DeprecationWarning,
            stacklevel=2,
        )
        warnings.simplefilter('default', DeprecationWarning)  # reset filter
        return func(*args, **kwargs)

    return new_func
