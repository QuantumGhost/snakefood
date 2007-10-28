"""
Parsing and finding routines.
This could be considered the core of snakefood, and where all the complexity lives.
"""

import sys, os, logging, traceback
import compiler
from compiler.visitor import ASTVisitor
from compiler.ast import Discard, Const
from os.path import *

from roots import find_package_root

__all__ = ('find_dependencies', 'find_imports',
           'ImportVisitor', 'get_local_names'
           'ERROR_IMPORT', 'ERROR_SYMBOL')



ERROR_IMPORT = "    Line %d: Could not import module '%s'"
ERROR_SYMBOL = "    Line %d: Symbol is not a module: '%s'"
ERROR_SOURCE = "       %s"
WARNING_OPTIONAL = "    Line %d: Pragma suppressing import '%s'"

def find_dependencies(fn, verbose, process_pragmas):
    "Returns a list of the files 'fn' depends on."
    file_errors = []

    found_modules = parse_python_source(fn)
    if found_modules is None:
        return [], file_errors

    output_code = (verbose >= 2)
    source_lines = None
    if output_code:
        source_lines = open(fn).read().splitlines()

    files = []
    assert not isdir(fn)
    dn = dirname(fn)
    seenset = set()
    for x in found_modules:
        mod, rname, lname, lineno, pragma = x
        if process_pragmas and pragma == 'OPTIONAL':
            logging.warning(WARNING_OPTIONAL %
                            (lineno, mod if rname is None else '%s.%s' % (mod, rname)))
            continue

        sig = (mod, rname)
        if sig in seenset:
            continue
        seenset.add(sig)

        modfile, errors = find_dotted_module(mod, rname, dn)
        if errors:
            file_errors.extend(errors)
            for err, name in errors:
                efun = logging.warning if err is ERROR_IMPORT else logging.debug
                efun(err % (lineno, name))
                if output_code:
                    efun(ERROR_SOURCE % source_lines[lineno-1].rstrip())

        if modfile is None:
            continue
        files.append(realpath(modfile))

    return files, file_errors

def find_imports(fn, verbose, ignores):
    "Yields a list of the module names the file 'fn' depends on."

    found_modules = parse_python_source(fn)
    if found_modules is None:
        raise StopIteration

    dn = dirname(fn)

    packroot = None
    for modname, rname, lname, lineno, _ in found_modules:
        islocal = False
        names = modname.split('.')
        if find_dotted(names, dn):
            # This is a local import, we need to find the root in order to
            # compute the absolute module name.
            if packroot is None:
                packroot = find_package_root(fn, ignores)
                if not packroot:
                    logging.warning(
                        "%d: Could not find package root for local import '%s' from '%s'." %
                        (lineno, modname, fn))
                    continue

            reldir = dirname(fn)[len(packroot)+1:]

            modname = '%s.%s' % (reldir.replace(os.sep, '.'), modname)
            islocal = True

        if rname is not None:
            modname = '%s.%s' % (modname, rname)
        yield (modname, lineno, islocal)


class ImportVisitor(object):
    """AST visitor for grabbing the import statements.

    This visitor produces a list of

       (module-name, remote-name, local-name, line-no, pragma)

    * remote-name is the name off the symbol in the imported module.
    * local-name is the name of the object given in the importing module.
    """
    def __init__(self):
        self.modules = []
        self.recent = []

    def visitImport(self, node):
        self.accept_imports()
        self.recent.extend((x[0], None, x[1] or x[0], node.lineno)
                           for x in node.names)

    def visitFrom(self, node):
        self.accept_imports()
        modname = node.modname
        if modname == '__future__':
            return # Ignore these.
        for name, as_ in node.names:
            if name == '*':
                # We really don't know...
                mod = (modname, None, None, node.lineno)
            else:
                mod = (modname, name, as_ or name, node.lineno)
            self.recent.append(mod)

    def default(self, node):
        pragma = None
        if self.recent:
            if isinstance(node, Discard):
                children = node.getChildren()
                if len(children) == 1 and isinstance(children[0], Const):
                    const_node = children[0]
                    pragma = const_node.value

        self.accept_imports(pragma)

    def accept_imports(self, pragma=None):
        self.modules.extend((m, r, l, n, pragma) for (m, r, l, n) in self.recent)
        self.recent = []

    def finalize(self):
        self.accept_imports()
        return self.modules


def check_duplicate_imports(found_imports):
    """
    Heuristically check for duplicate imports, and return two lists:
    a list of the unique imports and a list of the duplicates.
    """
    uniq, dups = [], []
    simp = set()
    for x in found_imports:
        modname, rname, lname, lineno, pragma = x
        key = (modname, rname)
        if key in simp:
            dups.append(x)
        else:
            uniq.append(x)
            simp.add(key)
    return uniq


def get_local_names(found_imports):
    """
    Convert the results of running the ImportVisitor into a simple list of local
    names.
    """
    return [(lname, no)
            for modname, rname, lname, no, pragma in found_imports
            if lname is not None]


class ImportWalker(ASTVisitor):
    "AST walker that we use to dispatch to a default method on the visitor."

    def __init__(self, visitor):
        ASTVisitor.__init__(self)
        self._visitor = visitor

    def default(self, node, *args):
        self._visitor.default(node)
        ASTVisitor.default(self, node, *args)


def parse_python_source(fn):
    """Parse the file 'fn' and return a list of module tuples, in the form:

        (modname, name, lineno, pragma)
    """
    try:
        mod = compiler.parseFile(fn)
    except Exception, e:
        logging.error("Error processing file '%s':\n\n%s" %
                      (fn, traceback.format_exc(sys.stderr)))
        return None

    vis = ImportVisitor()
    compiler.walk(mod, vis, ImportWalker(vis))
    modules = vis.finalize()

    return modules




# **WARNING** This is where all the evil lies.  Risk and peril.  Watch out.

libpath = join(sys.prefix, 'lib', 'python%d.%d' % sys.version_info[:2])

exceptions = ('os.path',)
builtin_module_names = sys.builtin_module_names + exceptions

module_cache = {}

def find_dotted_module(modname, rname, parentdir):
    """
    A version of find_module that supports dotted module names (packages).  This
    function returns the filename of the module if found, otherwise returns
    None.

    If 'rname' is not None, it first attempts to import 'modname.rname', and if it
    fails, it must therefore not be a module, so we look up 'modname' and return
    that instead.

    'parentdir' is the directory of the file that attempts to do the import.  We
    attempt to do a local import there first.
    """
    # Check for builtins.
    if modname in builtin_module_names:
        return join(libpath, modname), None

    errors = []
    names = modname.split('.')

    # Try relative import, then global imports.
    fn = find_dotted(names, parentdir)
    if not fn:
        try:
            fn = module_cache[modname]
        except KeyError:
            fn = find_dotted(names)
            module_cache[modname] = fn

        if not fn:
            errors.append((ERROR_IMPORT, modname))
            return None, errors

    # If this is a from-form, try the target symbol as a module.
    if rname:
        fn2 = find_dotted([rname], dirname(fn))
        if fn2:
            fn = fn2
        else:
            errors.append((ERROR_SYMBOL, '.'.join((modname, rname))))
            # Pass-thru and return the filename of the parent, which was found.

    return fn, errors

try:
    from imp import ImpImporter
except ImportError:
    from pkgutil import ImpImporter

def find_dotted(names, parentdir=None):
    """
    Dotted import.  'names' is a list of path components, 'parentdir' is the
    parent directory.
    """
    filename = None
    for name in names:
        mod = ImpImporter(parentdir).find_module(name)
        if not mod:
            break
        filename = mod.get_filename()
        if not filename:
            break
        parentdir = dirname(filename)
    else:
        return filename

