import functools
import inspect
import itertools
import sys
import warnings
from collections import defaultdict
from collections import deque
from types import TracebackType
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import Generator
from typing import Generic
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import TypeVar
from typing import Union

import attr
import py

import _pytest
from _pytest._code import getfslineno
from _pytest._code.code import FormattedExcinfo
from _pytest._code.code import TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.compat import _format_args
from _pytest.compat import _PytestWrapper
from _pytest.compat import get_real_func
from _pytest.compat import get_real_method
from _pytest.compat import getfuncargnames
from _pytest.compat import getimfunc
from _pytest.compat import getlocation
from _pytest.compat import is_generator
from _pytest.compat import NOTSET
from _pytest.compat import order_preserving_dict
from _pytest.compat import overload
from _pytest.compat import safe_getattr
from _pytest.compat import TYPE_CHECKING
from _pytest.config import _PluggyPlugin
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.deprecated import FILLFUNCARGS
from _pytest.deprecated import FIXTURE_POSITIONAL_ARGUMENTS
from _pytest.deprecated import FUNCARGNAMES
from _pytest.mark import ParameterSet
from _pytest.outcomes import fail
from _pytest.outcomes import TEST_OUTCOME

if TYPE_CHECKING:
    from typing import Deque
    from typing import NoReturn
    from typing import Type
    from typing_extensions import Literal

    from _pytest import nodes
    from _pytest.main import Session
    from _pytest.python import CallSpec2
    from _pytest.python import Function
    from _pytest.python import Metafunc

    _Scope = Literal["session", "package", "module", "class", "function"]


# The value of the fixture -- return/yield of the fixture function (type variable).
_FixtureValue = TypeVar("_FixtureValue")
# The type of the fixture function (type variable).
_FixtureFunction = TypeVar("_FixtureFunction", bound=Callable[..., object])
# The type of a fixture function (type alias generic in fixture value).
_FixtureFunc = Union[
    Callable[..., _FixtureValue], Callable[..., Generator[_FixtureValue, None, None]]
]
# The type of FixtureDef.cached_result (type alias generic in fixture value).
_FixtureCachedResult = Union[
    Tuple[
        # The result.
        _FixtureValue,
        # Cache key.
        object,
        None,
    ],
    Tuple[
        None,
        # Cache key.
        object,
        # Exc info if raised.
        Tuple["Type[BaseException]", BaseException, TracebackType],
    ],
]


@attr.s(frozen=True)
class PseudoFixtureDef:
    cached_result = attr.ib(type="_FixtureCachedResult")
    scope = attr.ib(type="_Scope")


def pytest_sessionstart(session: "Session") -> None:
    import _pytest.python
    import _pytest.nodes

    scopename2class.update(
        {
            "package": _pytest.python.Package,
            "class": _pytest.python.Class,
            "module": _pytest.python.Module,
            "function": _pytest.nodes.Item,
            "session": _pytest.main.Session,
        }
    )
    session._fixturemanager = FixtureManager(session)


scopename2class = {}  # type: Dict[str, Type[nodes.Node]]

scope2props = dict(session=())  # type: Dict[str, Tuple[str, ...]]
scope2props["package"] = ("fspath",)
scope2props["module"] = ("fspath", "module")
scope2props["class"] = scope2props["module"] + ("cls",)
scope2props["instance"] = scope2props["class"] + ("instance",)
scope2props["function"] = scope2props["instance"] + ("function", "keywords")


def scopeproperty(name=None, doc=None):
    def decoratescope(func):
        scopename = name or func.__name__

        def provide(self):
            if func.__name__ in scope2props[self.scope]:
                return func(self)
            raise AttributeError(
                "{} not available in {}-scoped context".format(scopename, self.scope)
            )

        return property(provide, None, None, func.__doc__)

    return decoratescope


def get_scope_package(node, fixturedef: "FixtureDef"):
    import pytest

    cls = pytest.Package
    current = node
    fixture_package_name = "{}/{}".format(fixturedef.baseid, "__init__.py")
    while current and (
        type(current) is not cls or fixture_package_name != current.nodeid
    ):
        current = current.parent
    if current is None:
        return node.session
    return current


def get_scope_node(node, scope):
    cls = scopename2class.get(scope)
    if cls is None:
        raise ValueError("unknown scope")
    return node.getparent(cls)


def add_funcarg_pseudo_fixture_def(
    collector, metafunc: "Metafunc", fixturemanager: "FixtureManager"
) -> None:
    # this function will transform all collected calls to a functions
    # if they use direct funcargs (i.e. direct parametrization)
    # because we want later test execution to be able to rely on
    # an existing FixtureDef structure for all arguments.
    # XXX we can probably avoid this algorithm  if we modify CallSpec2
    # to directly care for creating the fixturedefs within its methods.
    if not metafunc._calls[0].funcargs:
        return  # this function call does not have direct parametrization
    # collect funcargs of all callspecs into a list of values
    arg2params = {}  # type: Dict[str, List[object]]
    arg2scope = {}  # type: Dict[str, _Scope]
    for callspec in metafunc._calls:
        for argname, argvalue in callspec.funcargs.items():
            assert argname not in callspec.params
            callspec.params[argname] = argvalue
            arg2params_list = arg2params.setdefault(argname, [])
            callspec.indices[argname] = len(arg2params_list)
            arg2params_list.append(argvalue)
            if argname not in arg2scope:
                scopenum = callspec._arg2scopenum.get(argname, scopenum_function)
                arg2scope[argname] = scopes[scopenum]
        callspec.funcargs.clear()

    # register artificial FixtureDef's so that later at test execution
    # time we can rely on a proper FixtureDef to exist for fixture setup.
    arg2fixturedefs = metafunc._arg2fixturedefs
    for argname, valuelist in arg2params.items():
        # if we have a scope that is higher than function we need
        # to make sure we only ever create an according fixturedef on
        # a per-scope basis. We thus store and cache the fixturedef on the
        # node related to the scope.
        scope = arg2scope[argname]
        node = None
        if scope != "function":
            node = get_scope_node(collector, scope)
            if node is None:
                assert scope == "class" and isinstance(collector, _pytest.python.Module)
                # use module-level collector for class-scope (for now)
                node = collector
        if node and argname in node._name2pseudofixturedef:
            arg2fixturedefs[argname] = [node._name2pseudofixturedef[argname]]
        else:
            fixturedef = FixtureDef(
                fixturemanager=fixturemanager,
                baseid="",
                argname=argname,
                func=get_direct_param_fixture_func,
                scope=arg2scope[argname],
                params=valuelist,
                unittest=False,
                ids=None,
            )
            arg2fixturedefs[argname] = [fixturedef]
            if node is not None:
                node._name2pseudofixturedef[argname] = fixturedef


def getfixturemarker(obj: object) -> Optional["FixtureFunctionMarker"]:
    """ return fixturemarker or None if it doesn't exist or raised
    exceptions."""
    try:
        fixturemarker = getattr(
            obj, "_pytestfixturefunction", None
        )  # type: Optional[FixtureFunctionMarker]
    except TEST_OUTCOME:
        # some objects raise errors like request (from flask import request)
        # we don't expect them to be fixture functions
        return None
    return fixturemarker


# Parametrized fixture key, helper alias for code below.
_Key = Tuple[object, ...]


def get_parametrized_fixture_keys(item: "nodes.Item", scopenum: int) -> Iterator[_Key]:
    """ return list of keys for all parametrized arguments which match
    the specified scope. """
    assert scopenum < scopenum_function  # function
    try:
        callspec = item.callspec  # type: ignore[attr-defined]
    except AttributeError:
        pass
    else:
        cs = callspec  # type: CallSpec2
        # cs.indices.items() is random order of argnames.  Need to
        # sort this so that different calls to
        # get_parametrized_fixture_keys will be deterministic.
        for argname, param_index in sorted(cs.indices.items()):
            if cs._arg2scopenum[argname] != scopenum:
                continue
            if scopenum == 0:  # session
                key = (argname, param_index)  # type: _Key
            elif scopenum == 1:  # package
                key = (argname, param_index, item.fspath.dirpath())
            elif scopenum == 2:  # module
                key = (argname, param_index, item.fspath)
            elif scopenum == 3:  # class
                item_cls = item.cls  # type: ignore[attr-defined]
                key = (argname, param_index, item.fspath, item_cls)
            yield key


# algorithm for sorting on a per-parametrized resource setup basis
# it is called for scopenum==0 (session) first and performs sorting
# down to the lower scopes such as to minimize number of "high scope"
# setups and teardowns


def reorder_items(items: "Sequence[nodes.Item]") -> "List[nodes.Item]":
    argkeys_cache = {}  # type: Dict[int, Dict[nodes.Item, Dict[_Key, None]]]
    items_by_argkey = {}  # type: Dict[int, Dict[_Key, Deque[nodes.Item]]]
    for scopenum in range(0, scopenum_function):
        d = {}  # type: Dict[nodes.Item, Dict[_Key, None]]
        argkeys_cache[scopenum] = d
        item_d = defaultdict(deque)  # type: Dict[_Key, Deque[nodes.Item]]
        items_by_argkey[scopenum] = item_d
        for item in items:
            # cast is a workaround for https://github.com/python/typeshed/issues/3800.
            keys = cast(
                "Dict[_Key, None]",
                order_preserving_dict.fromkeys(
                    get_parametrized_fixture_keys(item, scopenum), None
                ),
            )
            if keys:
                d[item] = keys
                for key in keys:
                    item_d[key].append(item)
    # cast is a workaround for https://github.com/python/typeshed/issues/3800.
    items_dict = cast(
        "Dict[nodes.Item, None]", order_preserving_dict.fromkeys(items, None)
    )
    return list(reorder_items_atscope(items_dict, argkeys_cache, items_by_argkey, 0))


def fix_cache_order(
    item: "nodes.Item",
    argkeys_cache: "Dict[int, Dict[nodes.Item, Dict[_Key, None]]]",
    items_by_argkey: "Dict[int, Dict[_Key, Deque[nodes.Item]]]",
) -> None:
    for scopenum in range(0, scopenum_function):
        for key in argkeys_cache[scopenum].get(item, []):
            items_by_argkey[scopenum][key].appendleft(item)


def reorder_items_atscope(
    items: "Dict[nodes.Item, None]",
    argkeys_cache: "Dict[int, Dict[nodes.Item, Dict[_Key, None]]]",
    items_by_argkey: "Dict[int, Dict[_Key, Deque[nodes.Item]]]",
    scopenum: int,
) -> "Dict[nodes.Item, None]":
    if scopenum >= scopenum_function or len(items) < 3:
        return items
    ignore = set()  # type: Set[Optional[_Key]]
    items_deque = deque(items)
    items_done = order_preserving_dict()  # type: Dict[nodes.Item, None]
    scoped_items_by_argkey = items_by_argkey[scopenum]
    scoped_argkeys_cache = argkeys_cache[scopenum]
    while items_deque:
        no_argkey_group = order_preserving_dict()  # type: Dict[nodes.Item, None]
        slicing_argkey = None
        while items_deque:
            item = items_deque.popleft()
            if item in items_done or item in no_argkey_group:
                continue
            argkeys = order_preserving_dict.fromkeys(
                (k for k in scoped_argkeys_cache.get(item, []) if k not in ignore), None
            )
            if not argkeys:
                no_argkey_group[item] = None
            else:
                slicing_argkey, _ = argkeys.popitem()
                # we don't have to remove relevant items from later in the deque because they'll just be ignored
                matching_items = [
                    i for i in scoped_items_by_argkey[slicing_argkey] if i in items
                ]
                for i in reversed(matching_items):
                    fix_cache_order(i, argkeys_cache, items_by_argkey)
                    items_deque.appendleft(i)
                break
        if no_argkey_group:
            no_argkey_group = reorder_items_atscope(
                no_argkey_group, argkeys_cache, items_by_argkey, scopenum + 1
            )
            for item in no_argkey_group:
                items_done[item] = None
        ignore.add(slicing_argkey)
    return items_done


def fillfixtures(function: "Function") -> None:
    """ fill missing funcargs for a test function. """
    warnings.warn(FILLFUNCARGS, stacklevel=2)
    try:
        request = function._request
    except AttributeError:
        # XXX this special code path is only expected to execute
        # with the oejskit plugin.  It uses classes with funcargs
        # and we thus have to work a bit to allow this.
        fm = function.session._fixturemanager
        assert function.parent is not None
        fi = fm.getfixtureinfo(function.parent, function.obj, None)
        function._fixtureinfo = fi
        request = function._request = FixtureRequest(function)
        request._fillfixtures()
        # prune out funcargs for jstests
        newfuncargs = {}
        for name in fi.argnames:
            newfuncargs[name] = function.funcargs[name]
        function.funcargs = newfuncargs
    else:
        request._fillfixtures()


def get_direct_param_fixture_func(request):
    return request.param


@attr.s(slots=True)
class FuncFixtureInfo:
    # original function argument names
    argnames = attr.ib(type=Tuple[str, ...])
    # argnames that function immediately requires. These include argnames +
    # fixture names specified via usefixtures and via autouse=True in fixture
    # definitions.
    initialnames = attr.ib(type=Tuple[str, ...])
    names_closure = attr.ib(type=List[str])
    name2fixturedefs = attr.ib(type=Dict[str, Sequence["FixtureDef"]])

    def prune_dependency_tree(self) -> None:
        """Recompute names_closure from initialnames and name2fixturedefs

        Can only reduce names_closure, which means that the new closure will
        always be a subset of the old one. The order is preserved.

        This method is needed because direct parametrization may shadow some
        of the fixtures that were included in the originally built dependency
        tree. In this way the dependency tree can get pruned, and the closure
        of argnames may get reduced.
        """
        closure = set()  # type: Set[str]
        working_set = set(self.initialnames)
        while working_set:
            argname = working_set.pop()
            # argname may be smth not included in the original names_closure,
            # in which case we ignore it. This currently happens with pseudo
            # FixtureDefs which wrap 'get_direct_param_fixture_func(request)'.
            # So they introduce the new dependency 'request' which might have
            # been missing in the original tree (closure).
            if argname not in closure and argname in self.names_closure:
                closure.add(argname)
                if argname in self.name2fixturedefs:
                    working_set.update(self.name2fixturedefs[argname][-1].argnames)

        self.names_closure[:] = sorted(closure, key=self.names_closure.index)


class FixtureRequest:
    """ A request for a fixture from a test or fixture function.

    A request object gives access to the requesting test context
    and has an optional ``param`` attribute in case
    the fixture is parametrized indirectly.
    """

    def __init__(self, pyfuncitem) -> None:
        self._pyfuncitem = pyfuncitem
        #: fixture for which this request is being performed
        self.fixturename = None  # type: Optional[str]
        #: Scope string, one of "function", "class", "module", "session"
        self.scope = "function"  # type: _Scope
        self._fixture_defs = {}  # type: Dict[str, FixtureDef]
        fixtureinfo = pyfuncitem._fixtureinfo  # type: FuncFixtureInfo
        self._arg2fixturedefs = fixtureinfo.name2fixturedefs.copy()
        self._arg2index = {}  # type: Dict[str, int]
        self._fixturemanager = (
            pyfuncitem.session._fixturemanager
        )  # type: FixtureManager

    @property
    def fixturenames(self) -> List[str]:
        """names of all active fixtures in this request"""
        result = list(self._pyfuncitem._fixtureinfo.names_closure)
        result.extend(set(self._fixture_defs).difference(result))
        return result

    @property
    def funcargnames(self) -> List[str]:
        """ alias attribute for ``fixturenames`` for pre-2.3 compatibility"""
        warnings.warn(FUNCARGNAMES, stacklevel=2)
        return self.fixturenames

    @property
    def node(self):
        """ underlying collection node (depends on current request scope)"""
        return self._getscopeitem(self.scope)

    def _getnextfixturedef(self, argname: str) -> "FixtureDef":
        fixturedefs = self._arg2fixturedefs.get(argname, None)
        if fixturedefs is None:
            # we arrive here because of a dynamic call to
            # getfixturevalue(argname) usage which was naturally
            # not known at parsing/collection time
            assert self._pyfuncitem.parent is not None
            parentid = self._pyfuncitem.parent.nodeid
            fixturedefs = self._fixturemanager.getfixturedefs(argname, parentid)
            # TODO: Fix this type ignore. Either add assert or adjust types.
            #       Can this be None here?
            self._arg2fixturedefs[argname] = fixturedefs  # type: ignore[assignment]
        # fixturedefs list is immutable so we maintain a decreasing index
        index = self._arg2index.get(argname, 0) - 1
        if fixturedefs is None or (-index > len(fixturedefs)):
            raise FixtureLookupError(argname, self)
        self._arg2index[argname] = index
        return fixturedefs[index]

    @property
    def config(self) -> Config:
        """ the pytest config object associated with this request. """
        return self._pyfuncitem.config  # type: ignore[no-any-return] # noqa: F723

    @scopeproperty()
    def function(self):
        """ test function object if the request has a per-function scope. """
        return self._pyfuncitem.obj

    @scopeproperty("class")
    def cls(self):
        """ class (can be None) where the test function was collected. """
        clscol = self._pyfuncitem.getparent(_pytest.python.Class)
        if clscol:
            return clscol.obj

    @property
    def instance(self):
        """ instance (can be None) on which test function was collected. """
        # unittest support hack, see _pytest.unittest.TestCaseFunction
        try:
            return self._pyfuncitem._testcase
        except AttributeError:
            function = getattr(self, "function", None)
            return getattr(function, "__self__", None)

    @scopeproperty()
    def module(self):
        """ python module object where the test function was collected. """
        return self._pyfuncitem.getparent(_pytest.python.Module).obj

    @scopeproperty()
    def fspath(self) -> py.path.local:
        """ the file system path of the test module which collected this test. """
        # TODO: Remove ignore once _pyfuncitem is properly typed.
        return self._pyfuncitem.fspath  # type: ignore

    @property
    def keywords(self):
        """ keywords/markers dictionary for the underlying node. """
        return self.node.keywords

    @property
    def session(self):
        """ pytest session object. """
        return self._pyfuncitem.session

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        """ add finalizer/teardown function to be called after the
        last test within the requesting test context finished
        execution. """
        # XXX usually this method is shadowed by fixturedef specific ones
        self._addfinalizer(finalizer, scope=self.scope)

    def _addfinalizer(self, finalizer: Callable[[], object], scope) -> None:
        colitem = self._getscopeitem(scope)
        self._pyfuncitem.session._setupstate.addfinalizer(
            finalizer=finalizer, colitem=colitem
        )

    def applymarker(self, marker) -> None:
        """ Apply a marker to a single test function invocation.
        This method is useful if you don't want to have a keyword/marker
        on all function invocations.

        :arg marker: a :py:class:`_pytest.mark.MarkDecorator` object
            created by a call to ``pytest.mark.NAME(...)``.
        """
        self.node.add_marker(marker)

    def raiseerror(self, msg: Optional[str]) -> "NoReturn":
        """ raise a FixtureLookupError with the given message. """
        raise self._fixturemanager.FixtureLookupError(None, self, msg)

    def _fillfixtures(self) -> None:
        item = self._pyfuncitem
        fixturenames = getattr(item, "fixturenames", self.fixturenames)
        for argname in fixturenames:
            if argname not in item.funcargs:
                item.funcargs[argname] = self.getfixturevalue(argname)

    def getfixturevalue(self, argname: str) -> Any:
        """ Dynamically run a named fixture function.

        Declaring fixtures via function argument is recommended where possible.
        But if you can only decide whether to use another fixture at test
        setup time, you may use this function to retrieve it inside a fixture
        or test function body.

        :raise pytest.FixtureLookupError:
            If the given fixture could not be found.
        """
        fixturedef = self._get_active_fixturedef(argname)
        assert fixturedef.cached_result is not None
        return fixturedef.cached_result[0]

    def _get_active_fixturedef(
        self, argname: str
    ) -> Union["FixtureDef", PseudoFixtureDef]:
        try:
            return self._fixture_defs[argname]
        except KeyError:
            try:
                fixturedef = self._getnextfixturedef(argname)
            except FixtureLookupError:
                if argname == "request":
                    cached_result = (self, [0], None)
                    scope = "function"  # type: _Scope
                    return PseudoFixtureDef(cached_result, scope)
                raise
        # remove indent to prevent the python3 exception
        # from leaking into the call
        self._compute_fixture_value(fixturedef)
        self._fixture_defs[argname] = fixturedef
        return fixturedef

    def _get_fixturestack(self) -> List["FixtureDef"]:
        current = self
        values = []  # type: List[FixtureDef]
        while 1:
            fixturedef = getattr(current, "_fixturedef", None)
            if fixturedef is None:
                values.reverse()
                return values
            values.append(fixturedef)
            assert isinstance(current, SubRequest)
            current = current._parent_request

    def _compute_fixture_value(self, fixturedef: "FixtureDef") -> None:
        """
        Creates a SubRequest based on "self" and calls the execute method of the given fixturedef object. This will
        force the FixtureDef object to throw away any previous results and compute a new fixture value, which
        will be stored into the FixtureDef object itself.
        """
        # prepare a subrequest object before calling fixture function
        # (latter managed by fixturedef)
        argname = fixturedef.argname
        funcitem = self._pyfuncitem
        scope = fixturedef.scope
        try:
            param = funcitem.callspec.getparam(argname)
        except (AttributeError, ValueError):
            param = NOTSET
            param_index = 0
            has_params = fixturedef.params is not None
            fixtures_not_supported = getattr(funcitem, "nofuncargs", False)
            if has_params and fixtures_not_supported:
                msg = (
                    "{name} does not support fixtures, maybe unittest.TestCase subclass?\n"
                    "Node id: {nodeid}\n"
                    "Function type: {typename}"
                ).format(
                    name=funcitem.name,
                    nodeid=funcitem.nodeid,
                    typename=type(funcitem).__name__,
                )
                fail(msg, pytrace=False)
            if has_params:
                frame = inspect.stack()[3]
                frameinfo = inspect.getframeinfo(frame[0])
                source_path = py.path.local(frameinfo.filename)
                source_lineno = frameinfo.lineno
                rel_source_path = source_path.relto(funcitem.config.rootdir)
                if rel_source_path:
                    source_path_str = rel_source_path
                else:
                    source_path_str = str(source_path)
                msg = (
                    "The requested fixture has no parameter defined for test:\n"
                    "    {}\n\n"
                    "Requested fixture '{}' defined in:\n{}"
                    "\n\nRequested here:\n{}:{}".format(
                        funcitem.nodeid,
                        fixturedef.argname,
                        getlocation(fixturedef.func, funcitem.config.rootdir),
                        source_path_str,
                        source_lineno,
                    )
                )
                fail(msg, pytrace=False)
        else:
            param_index = funcitem.callspec.indices[argname]
            # if a parametrize invocation set a scope it will override
            # the static scope defined with the fixture function
            paramscopenum = funcitem.callspec._arg2scopenum.get(argname)
            if paramscopenum is not None:
                scope = scopes[paramscopenum]

        subrequest = SubRequest(self, scope, param, param_index, fixturedef)

        # check if a higher-level scoped fixture accesses a lower level one
        subrequest._check_scope(argname, self.scope, scope)
        try:
            # call the fixture function
            fixturedef.execute(request=subrequest)
        finally:
            self._schedule_finalizers(fixturedef, subrequest)

    def _schedule_finalizers(
        self, fixturedef: "FixtureDef", subrequest: "SubRequest"
    ) -> None:
        # if fixture function failed it might have registered finalizers
        self.session._setupstate.addfinalizer(
            functools.partial(fixturedef.finish, request=subrequest), subrequest.node
        )

    def _check_scope(self, argname, invoking_scope: "_Scope", requested_scope) -> None:
        if argname == "request":
            return
        if scopemismatch(invoking_scope, requested_scope):
            # try to report something helpful
            lines = self._factorytraceback()
            fail(
                "ScopeMismatch: You tried to access the %r scoped "
                "fixture %r with a %r scoped request object, "
                "involved factories\n%s"
                % ((requested_scope, argname, invoking_scope, "\n".join(lines))),
                pytrace=False,
            )

    def _factorytraceback(self) -> List[str]:
        lines = []
        for fixturedef in self._get_fixturestack():
            factory = fixturedef.func
            fs, lineno = getfslineno(factory)
            p = self._pyfuncitem.session.fspath.bestrelpath(fs)
            args = _format_args(factory)
            lines.append("%s:%d:  def %s%s" % (p, lineno + 1, factory.__name__, args))
        return lines

    def _getscopeitem(self, scope):
        if scope == "function":
            # this might also be a non-function Item despite its attribute name
            return self._pyfuncitem
        if scope == "package":
            # FIXME: _fixturedef is not defined on FixtureRequest (this class),
            # but on FixtureRequest (a subclass).
            node = get_scope_package(self._pyfuncitem, self._fixturedef)  # type: ignore[attr-defined]
        else:
            node = get_scope_node(self._pyfuncitem, scope)
        if node is None and scope == "class":
            # fallback to function item itself
            node = self._pyfuncitem
        assert node, 'Could not obtain a node for scope "{}" for function {!r}'.format(
            scope, self._pyfuncitem
        )
        return node

    def __repr__(self) -> str:
        return "<FixtureRequest for %r>" % (self.node)


class SubRequest(FixtureRequest):
    """ a sub request for handling getting a fixture from a
    test function/fixture. """

    def __init__(
        self,
        request: "FixtureRequest",
        scope: "_Scope",
        param,
        param_index: int,
        fixturedef: "FixtureDef",
    ) -> None:
        self._parent_request = request
        self.fixturename = fixturedef.argname  # type: str
        if param is not NOTSET:
            self.param = param
        self.param_index = param_index
        self.scope = scope
        self._fixturedef = fixturedef
        self._pyfuncitem = request._pyfuncitem
        self._fixture_defs = request._fixture_defs
        self._arg2fixturedefs = request._arg2fixturedefs
        self._arg2index = request._arg2index
        self._fixturemanager = request._fixturemanager

    def __repr__(self) -> str:
        return "<SubRequest {!r} for {!r}>".format(self.fixturename, self._pyfuncitem)

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self._fixturedef.addfinalizer(finalizer)

    def _schedule_finalizers(
        self, fixturedef: "FixtureDef", subrequest: "SubRequest"
    ) -> None:
        # if the executing fixturedef was not explicitly requested in the argument list (via
        # getfixturevalue inside the fixture call) then ensure this fixture def will be finished
        # first
        if fixturedef.argname not in self.fixturenames:
            fixturedef.addfinalizer(
                functools.partial(self._fixturedef.finish, request=self)
            )
        super()._schedule_finalizers(fixturedef, subrequest)


scopes = ["session", "package", "module", "class", "function"]  # type: List[_Scope]
scopenum_function = scopes.index("function")


def scopemismatch(currentscope: "_Scope", newscope: "_Scope") -> bool:
    return scopes.index(newscope) > scopes.index(currentscope)


def scope2index(scope: str, descr: str, where: Optional[str] = None) -> int:
    """Look up the index of ``scope`` and raise a descriptive value error
    if not defined.
    """
    strscopes = scopes  # type: Sequence[str]
    try:
        return strscopes.index(scope)
    except ValueError:
        fail(
            "{} {}got an unexpected scope value '{}'".format(
                descr, "from {} ".format(where) if where else "", scope
            ),
            pytrace=False,
        )


class FixtureLookupError(LookupError):
    """ could not return a requested Fixture (missing or invalid). """

    def __init__(
        self, argname: Optional[str], request: FixtureRequest, msg: Optional[str] = None
    ) -> None:
        self.argname = argname
        self.request = request
        self.fixturestack = request._get_fixturestack()
        self.msg = msg

    def formatrepr(self) -> "FixtureLookupErrorRepr":
        tblines = []  # type: List[str]
        addline = tblines.append
        stack = [self.request._pyfuncitem.obj]
        stack.extend(map(lambda x: x.func, self.fixturestack))
        msg = self.msg
        if msg is not None:
            # the last fixture raise an error, let's present
            # it at the requesting side
            stack = stack[:-1]
        for function in stack:
            fspath, lineno = getfslineno(function)
            try:
                lines, _ = inspect.getsourcelines(get_real_func(function))
            except (OSError, IndexError, TypeError):
                error_msg = "file %s, line %s: source code not available"
                addline(error_msg % (fspath, lineno + 1))
            else:
                addline("file {}, line {}".format(fspath, lineno + 1))
                for i, line in enumerate(lines):
                    line = line.rstrip()
                    addline("  " + line)
                    if line.lstrip().startswith("def"):
                        break

        if msg is None:
            fm = self.request._fixturemanager
            available = set()
            parentid = self.request._pyfuncitem.parent.nodeid
            for name, fixturedefs in fm._arg2fixturedefs.items():
                faclist = list(fm._matchfactories(fixturedefs, parentid))
                if faclist:
                    available.add(name)
            if self.argname in available:
                msg = " recursive dependency involving fixture '{}' detected".format(
                    self.argname
                )
            else:
                msg = "fixture '{}' not found".format(self.argname)
            msg += "\n available fixtures: {}".format(", ".join(sorted(available)))
            msg += "\n use 'pytest --fixtures [testpath]' for help on them."

        return FixtureLookupErrorRepr(fspath, lineno, tblines, msg, self.argname)


class FixtureLookupErrorRepr(TerminalRepr):
    def __init__(
        self,
        filename: Union[str, py.path.local],
        firstlineno: int,
        tblines: Sequence[str],
        errorstring: str,
        argname: Optional[str],
    ) -> None:
        self.tblines = tblines
        self.errorstring = errorstring
        self.filename = filename
        self.firstlineno = firstlineno
        self.argname = argname

    def toterminal(self, tw: TerminalWriter) -> None:
        # tw.line("FixtureLookupError: %s" %(self.argname), red=True)
        for tbline in self.tblines:
            tw.line(tbline.rstrip())
        lines = self.errorstring.split("\n")
        if lines:
            tw.line(
                "{}       {}".format(FormattedExcinfo.fail_marker, lines[0].strip()),
                red=True,
            )
            for line in lines[1:]:
                tw.line(
                    "{}       {}".format(FormattedExcinfo.flow_marker, line.strip()),
                    red=True,
                )
        tw.line()
        tw.line("%s:%d" % (self.filename, self.firstlineno + 1))


def fail_fixturefunc(fixturefunc, msg: str) -> "NoReturn":
    fs, lineno = getfslineno(fixturefunc)
    location = "{}:{}".format(fs, lineno + 1)
    source = _pytest._code.Source(fixturefunc)
    fail(msg + ":\n\n" + str(source.indent()) + "\n" + location, pytrace=False)


def call_fixture_func(
    fixturefunc: "_FixtureFunc[_FixtureValue]", request: FixtureRequest, kwargs
) -> _FixtureValue:
    if is_generator(fixturefunc):
        fixturefunc = cast(
            Callable[..., Generator[_FixtureValue, None, None]], fixturefunc
        )
        generator = fixturefunc(**kwargs)
        try:
            fixture_result = next(generator)
        except StopIteration:
            raise ValueError(
                "{} did not yield a value".format(request.fixturename)
            ) from None
        finalizer = functools.partial(_teardown_yield_fixture, fixturefunc, generator)
        request.addfinalizer(finalizer)
    else:
        fixturefunc = cast(Callable[..., _FixtureValue], fixturefunc)
        fixture_result = fixturefunc(**kwargs)
    return fixture_result


def _teardown_yield_fixture(fixturefunc, it) -> None:
    """Executes the teardown of a fixture function by advancing the iterator after the
    yield and ensure the iteration ends (if not it means there is more than one yield in the function)"""
    try:
        next(it)
    except StopIteration:
        pass
    else:
        fail_fixturefunc(fixturefunc, "fixture function has more than one 'yield'")


def _eval_scope_callable(
    scope_callable: "Callable[[str, Config], _Scope]",
    fixture_name: str,
    config: Config,
) -> "_Scope":
    try:
        # Type ignored because there is no typing mechanism to specify
        # keyword arguments, currently.
        result = scope_callable(fixture_name=fixture_name, config=config)  # type: ignore[call-arg]
    except Exception as e:
        raise TypeError(
            "Error evaluating {} while defining fixture '{}'.\n"
            "Expected a function with the signature (*, fixture_name, config)".format(
                scope_callable, fixture_name
            )
        ) from e
    if not isinstance(result, str):
        fail(
            "Expected {} to return a 'str' while defining fixture '{}', but it returned:\n"
            "{!r}".format(scope_callable, fixture_name, result),
            pytrace=False,
        )
    return result


class FixtureDef(Generic[_FixtureValue]):
    """ A container for a factory definition. """

    def __init__(
        self,
        fixturemanager: "FixtureManager",
        baseid,
        argname: str,
        func: "_FixtureFunc[_FixtureValue]",
        scope: "Union[_Scope, Callable[[str, Config], _Scope]]",
        params: Optional[Sequence[object]],
        unittest: bool = False,
        ids: Optional[
            Union[
                Tuple[Union[None, str, float, int, bool], ...],
                Callable[[object], Optional[object]],
            ]
        ] = None,
    ) -> None:
        self._fixturemanager = fixturemanager
        self.baseid = baseid or ""
        self.has_location = baseid is not None
        self.func = func
        self.argname = argname
        if callable(scope):
            scope_ = _eval_scope_callable(scope, argname, fixturemanager.config)
        else:
            scope_ = scope
        self.scopenum = scope2index(
            scope_ or "function",
            descr="Fixture '{}'".format(func.__name__),
            where=baseid,
        )
        self.scope = scope_
        self.params = params  # type: Optional[Sequence[object]]
        self.argnames = getfuncargnames(
            func, name=argname, is_method=unittest
        )  # type: Tuple[str, ...]
        self.unittest = unittest
        self.ids = ids
        self.cached_result = None  # type: Optional[_FixtureCachedResult[_FixtureValue]]
        self._finalizers = []  # type: List[Callable[[], object]]

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self._finalizers.append(finalizer)

    def finish(self, request: SubRequest) -> None:
        exc = None
        try:
            while self._finalizers:
                try:
                    func = self._finalizers.pop()
                    func()
                except BaseException as e:
                    # XXX Only first exception will be seen by user,
                    #     ideally all should be reported.
                    if exc is None:
                        exc = e
            if exc:
                raise exc
        finally:
            hook = self._fixturemanager.session.gethookproxy(request.node.fspath)
            hook.pytest_fixture_post_finalizer(fixturedef=self, request=request)
            # even if finalization fails, we invalidate
            # the cached fixture value and remove
            # all finalizers because they may be bound methods which will
            # keep instances alive
            self.cached_result = None
            self._finalizers = []

    def execute(self, request: SubRequest) -> _FixtureValue:
        # get required arguments and register our own finish()
        # with their finalization
        for argname in self.argnames:
            fixturedef = request._get_active_fixturedef(argname)
            if argname != "request":
                # PseudoFixtureDef is only for "request".
                assert isinstance(fixturedef, FixtureDef)
                fixturedef.addfinalizer(functools.partial(self.finish, request=request))

        my_cache_key = self.cache_key(request)
        if self.cached_result is not None:
            # note: comparison with `==` can fail (or be expensive) for e.g.
            # numpy arrays (#6497)
            cache_key = self.cached_result[1]
            if my_cache_key is cache_key:
                if self.cached_result[2] is not None:
                    _, val, tb = self.cached_result[2]
                    raise val.with_traceback(tb)
                else:
                    result = self.cached_result[0]
                    return result
            # we have a previous but differently parametrized fixture instance
            # so we need to tear it down before creating a new one
            self.finish(request)
            assert self.cached_result is None

        hook = self._fixturemanager.session.gethookproxy(request.node.fspath)
        result = hook.pytest_fixture_setup(fixturedef=self, request=request)
        return result

    def cache_key(self, request: SubRequest) -> object:
        return request.param_index if not hasattr(request, "param") else request.param

    def __repr__(self) -> str:
        return "<FixtureDef argname={!r} scope={!r} baseid={!r}>".format(
            self.argname, self.scope, self.baseid
        )


def resolve_fixture_function(
    fixturedef: FixtureDef[_FixtureValue], request: FixtureRequest
) -> "_FixtureFunc[_FixtureValue]":
    """Gets the actual callable that can be called to obtain the fixture value, dealing with unittest-specific
    instances and bound methods.
    """
    fixturefunc = fixturedef.func
    if fixturedef.unittest:
        if request.instance is not None:
            # bind the unbound method to the TestCase instance
            fixturefunc = fixturedef.func.__get__(request.instance)  # type: ignore[union-attr]
    else:
        # the fixture function needs to be bound to the actual
        # request.instance so that code working with "fixturedef" behaves
        # as expected.
        if request.instance is not None:
            # handle the case where fixture is defined not in a test class, but some other class
            # (for example a plugin class with a fixture), see #2270
            if hasattr(fixturefunc, "__self__") and not isinstance(
                request.instance, fixturefunc.__self__.__class__  # type: ignore[union-attr]
            ):
                return fixturefunc
            fixturefunc = getimfunc(fixturedef.func)
            if fixturefunc != fixturedef.func:
                fixturefunc = fixturefunc.__get__(request.instance)  # type: ignore[union-attr]
    return fixturefunc


def pytest_fixture_setup(
    fixturedef: FixtureDef[_FixtureValue], request: SubRequest
) -> _FixtureValue:
    """ Execution of fixture setup. """
    kwargs = {}
    for argname in fixturedef.argnames:
        fixdef = request._get_active_fixturedef(argname)
        assert fixdef.cached_result is not None
        result, arg_cache_key, exc = fixdef.cached_result
        request._check_scope(argname, request.scope, fixdef.scope)
        kwargs[argname] = result

    fixturefunc = resolve_fixture_function(fixturedef, request)
    my_cache_key = fixturedef.cache_key(request)
    try:
        result = call_fixture_func(fixturefunc, request, kwargs)
    except TEST_OUTCOME:
        exc_info = sys.exc_info()
        assert exc_info[0] is not None
        fixturedef.cached_result = (None, my_cache_key, exc_info)
        raise
    fixturedef.cached_result = (result, my_cache_key, None)
    return result


def _ensure_immutable_ids(
    ids: Optional[
        Union[
            Iterable[Union[None, str, float, int, bool]],
            Callable[[object], Optional[object]],
        ]
    ],
) -> Optional[
    Union[
        Tuple[Union[None, str, float, int, bool], ...],
        Callable[[object], Optional[object]],
    ]
]:
    if ids is None:
        return None
    if callable(ids):
        return ids
    return tuple(ids)


def _params_converter(
    params: Optional[Iterable[object]],
) -> Optional[Tuple[object, ...]]:
    return tuple(params) if params is not None else None


def wrap_function_to_error_out_if_called_directly(function, fixture_marker):
    """Wrap the given fixture function so we can raise an error about it being called directly,
    instead of used as an argument in a test function.
    """
    message = (
        'Fixture "{name}" called directly. Fixtures are not meant to be called directly,\n'
        "but are created automatically when test functions request them as parameters.\n"
        "See https://docs.pytest.org/en/stable/fixture.html for more information about fixtures, and\n"
        "https://docs.pytest.org/en/stable/deprecations.html#calling-fixtures-directly about how to update your code."
    ).format(name=fixture_marker.name or function.__name__)

    @functools.wraps(function)
    def result(*args, **kwargs):
        fail(message, pytrace=False)

    # keep reference to the original function in our own custom attribute so we don't unwrap
    # further than this point and lose useful wrappings like @mock.patch (#3774)
    result.__pytest_wrapped__ = _PytestWrapper(function)  # type: ignore[attr-defined]

    return result


@attr.s(frozen=True)
class FixtureFunctionMarker:
    scope = attr.ib(type="Union[_Scope, Callable[[str, Config], _Scope]]")
    params = attr.ib(type=Optional[Tuple[object, ...]], converter=_params_converter)
    autouse = attr.ib(type=bool, default=False)
    ids = attr.ib(
        type=Union[
            Tuple[Union[None, str, float, int, bool], ...],
            Callable[[object], Optional[object]],
        ],
        default=None,
        converter=_ensure_immutable_ids,
    )
    name = attr.ib(type=Optional[str], default=None)

    def __call__(self, function: _FixtureFunction) -> _FixtureFunction:
        if inspect.isclass(function):
            raise ValueError("class fixtures not supported (maybe in the future)")

        if getattr(function, "_pytestfixturefunction", False):
            raise ValueError(
                "fixture is being applied more than once to the same function"
            )

        function = wrap_function_to_error_out_if_called_directly(function, self)

        name = self.name or function.__name__
        if name == "request":
            location = getlocation(function)
            fail(
                "'request' is a reserved word for fixtures, use another name:\n  {}".format(
                    location
                ),
                pytrace=False,
            )

        # Type ignored because https://github.com/python/mypy/issues/2087.
        function._pytestfixturefunction = self  # type: ignore[attr-defined]
        return function


@overload
def fixture(
    fixture_function: _FixtureFunction,
    *,
    scope: "Union[_Scope, Callable[[str, Config], _Scope]]" = ...,
    params: Optional[Iterable[object]] = ...,
    autouse: bool = ...,
    ids: Optional[
        Union[
            Iterable[Union[None, str, float, int, bool]],
            Callable[[object], Optional[object]],
        ]
    ] = ...,
    name: Optional[str] = ...
) -> _FixtureFunction:
    raise NotImplementedError()


@overload  # noqa: F811
def fixture(  # noqa: F811
    fixture_function: None = ...,
    *,
    scope: "Union[_Scope, Callable[[str, Config], _Scope]]" = ...,
    params: Optional[Iterable[object]] = ...,
    autouse: bool = ...,
    ids: Optional[
        Union[
            Iterable[Union[None, str, float, int, bool]],
            Callable[[object], Optional[object]],
        ]
    ] = ...,
    name: Optional[str] = None
) -> FixtureFunctionMarker:
    raise NotImplementedError()


def fixture(  # noqa: F811
    fixture_function: Optional[_FixtureFunction] = None,
    *args: Any,
    scope: "Union[_Scope, Callable[[str, Config], _Scope]]" = "function",
    params: Optional[Iterable[object]] = None,
    autouse: bool = False,
    ids: Optional[
        Union[
            Iterable[Union[None, str, float, int, bool]],
            Callable[[object], Optional[object]],
        ]
    ] = None,
    name: Optional[str] = None
) -> Union[FixtureFunctionMarker, _FixtureFunction]:
    """Decorator to mark a fixture factory function.

    This decorator can be used, with or without parameters, to define a
    fixture function.

    The name of the fixture function can later be referenced to cause its
    invocation ahead of running tests: test
    modules or classes can use the ``pytest.mark.usefixtures(fixturename)``
    marker.

    Test functions can directly use fixture names as input
    arguments in which case the fixture instance returned from the fixture
    function will be injected.

    Fixtures can provide their values to test functions using ``return`` or ``yield``
    statements. When using ``yield`` the code block after the ``yield`` statement is executed
    as teardown code regardless of the test outcome, and must yield exactly once.

    :arg scope: the scope for which this fixture is shared, one of
                ``"function"`` (default), ``"class"``, ``"module"``,
                ``"package"`` or ``"session"``.

                This parameter may also be a callable which receives ``(fixture_name, config)``
                as parameters, and must return a ``str`` with one of the values mentioned above.

                See :ref:`dynamic scope` in the docs for more information.

    :arg params: an optional list of parameters which will cause multiple
                invocations of the fixture function and all of the tests
                using it.
                The current parameter is available in ``request.param``.

    :arg autouse: if True, the fixture func is activated for all tests that
                can see it.  If False (the default) then an explicit
                reference is needed to activate the fixture.

    :arg ids: list of string ids each corresponding to the params
                so that they are part of the test id. If no ids are provided
                they will be generated automatically from the params.

    :arg name: the name of the fixture. This defaults to the name of the
                decorated function. If a fixture is used in the same module in
                which it is defined, the function name of the fixture will be
                shadowed by the function arg that requests the fixture; one way
                to resolve this is to name the decorated function
                ``fixture_<fixturename>`` and then use
                ``@pytest.fixture(name='<fixturename>')``.
    """
    # Positional arguments backward compatibility.
    # If a kwarg is equal to its default, assume it was not explicitly
    # passed, i.e. not duplicated. The more correct way is to use a
    # **kwargs and check `in`, but that obfuscates the function signature.
    if isinstance(fixture_function, str):
        # It's actually the first positional argument, scope.
        args = (fixture_function, *args)
        fixture_function = None
    duplicated_args = []
    if len(args) > 0:
        if scope == "function":
            scope = args[0]
        else:
            duplicated_args.append("scope")
    if len(args) > 1:
        if params is None:
            params = args[1]
        else:
            duplicated_args.append("params")
    if len(args) > 2:
        if autouse is False:
            autouse = args[2]
        else:
            duplicated_args.append("autouse")
    if len(args) > 3:
        if ids is None:
            ids = args[3]
        else:
            duplicated_args.append("ids")
    if len(args) > 4:
        if name is None:
            name = args[4]
        else:
            duplicated_args.append("name")
    if len(args) > 5:
        raise TypeError(
            "fixture() takes 5 positional arguments but {} were given".format(len(args))
        )
    if duplicated_args:
        raise TypeError(
            "The fixture arguments are defined as positional and keyword: {}. "
            "Use only keyword arguments.".format(", ".join(duplicated_args))
        )
    if args:
        warnings.warn(FIXTURE_POSITIONAL_ARGUMENTS, stacklevel=2)
    # End backward compatiblity.

    fixture_marker = FixtureFunctionMarker(
        scope=scope, params=params, autouse=autouse, ids=ids, name=name,
    )

    # Direct decoration.
    if fixture_function:
        return fixture_marker(fixture_function)

    return fixture_marker


def yield_fixture(
    fixture_function=None,
    *args,
    scope="function",
    params=None,
    autouse=False,
    ids=None,
    name=None
):
    """ (return a) decorator to mark a yield-fixture factory function.

    .. deprecated:: 3.0
        Use :py:func:`pytest.fixture` directly instead.
    """
    return fixture(
        fixture_function,
        *args,
        scope=scope,
        params=params,
        autouse=autouse,
        ids=ids,
        name=name,
    )


@fixture(scope="session")
def pytestconfig(request: FixtureRequest) -> Config:
    """Session-scoped fixture that returns the :class:`_pytest.config.Config` object.

    Example::

        def test_foo(pytestconfig):
            if pytestconfig.getoption("verbose") > 0:
                ...

    """
    return request.config


def pytest_addoption(parser: Parser) -> None:
    parser.addini(
        "usefixtures",
        type="args",
        default=[],
        help="list of default fixtures to be used with this project",
    )


class FixtureManager:
    """
    pytest fixtures definitions and information is stored and managed
    from this class.

    During collection fm.parsefactories() is called multiple times to parse
    fixture function definitions into FixtureDef objects and internal
    data structures.

    During collection of test functions, metafunc-mechanics instantiate
    a FuncFixtureInfo object which is cached per node/func-name.
    This FuncFixtureInfo object is later retrieved by Function nodes
    which themselves offer a fixturenames attribute.

    The FuncFixtureInfo object holds information about fixtures and FixtureDefs
    relevant for a particular function.  An initial list of fixtures is
    assembled like this:

    - ini-defined usefixtures
    - autouse-marked fixtures along the collection chain up from the function
    - usefixtures markers at module/class/function level
    - test function funcargs

    Subsequently the funcfixtureinfo.fixturenames attribute is computed
    as the closure of the fixtures needed to setup the initial fixtures,
    i. e. fixtures needed by fixture functions themselves are appended
    to the fixturenames list.

    Upon the test-setup phases all fixturenames are instantiated, retrieved
    by a lookup of their FuncFixtureInfo.
    """

    FixtureLookupError = FixtureLookupError
    FixtureLookupErrorRepr = FixtureLookupErrorRepr

    def __init__(self, session: "Session") -> None:
        self.session = session
        self.config = session.config  # type: Config
        self._arg2fixturedefs = {}  # type: Dict[str, List[FixtureDef]]
        self._holderobjseen = set()  # type: Set
        self._nodeid_and_autousenames = [
            ("", self.config.getini("usefixtures"))
        ]  # type: List[Tuple[str, List[str]]]
        session.config.pluginmanager.register(self, "funcmanage")

    def _get_direct_parametrize_args(self, node: "nodes.Node") -> List[str]:
        """This function returns all the direct parametrization
        arguments of a node, so we don't mistake them for fixtures

        Check https://github.com/pytest-dev/pytest/issues/5036

        This things are done later as well when dealing with parametrization
        so this could be improved
        """
        parametrize_argnames = []  # type: List[str]
        for marker in node.iter_markers(name="parametrize"):
            if not marker.kwargs.get("indirect", False):
                p_argnames, _ = ParameterSet._parse_parametrize_args(
                    *marker.args, **marker.kwargs
                )
                parametrize_argnames.extend(p_argnames)

        return parametrize_argnames

    def getfixtureinfo(
        self, node: "nodes.Node", func, cls, funcargs: bool = True
    ) -> FuncFixtureInfo:
        if funcargs and not getattr(node, "nofuncargs", False):
            argnames = getfuncargnames(func, name=node.name, cls=cls)
        else:
            argnames = ()

        usefixtures = itertools.chain.from_iterable(
            mark.args for mark in node.iter_markers(name="usefixtures")
        )
        initialnames = tuple(usefixtures) + argnames
        fm = node.session._fixturemanager
        initialnames, names_closure, arg2fixturedefs = fm.getfixtureclosure(
            initialnames, node, ignore_args=self._get_direct_parametrize_args(node)
        )
        return FuncFixtureInfo(argnames, initialnames, names_closure, arg2fixturedefs)

    def pytest_plugin_registered(self, plugin: _PluggyPlugin) -> None:
        nodeid = None
        try:
            p = py.path.local(plugin.__file__)  # type: ignore[attr-defined]
        except AttributeError:
            pass
        else:
            from _pytest import nodes

            # construct the base nodeid which is later used to check
            # what fixtures are visible for particular tests (as denoted
            # by their test id)
            if p.basename.startswith("conftest.py"):
                nodeid = p.dirpath().relto(self.config.rootdir)
                if p.sep != nodes.SEP:
                    nodeid = nodeid.replace(p.sep, nodes.SEP)

        self.parsefactories(plugin, nodeid)

    def _getautousenames(self, nodeid: str) -> List[str]:
        """ return a tuple of fixture names to be used. """
        autousenames = []  # type: List[str]
        for baseid, basenames in self._nodeid_and_autousenames:
            if nodeid.startswith(baseid):
                if baseid:
                    i = len(baseid)
                    nextchar = nodeid[i : i + 1]
                    if nextchar and nextchar not in ":/":
                        continue
                autousenames.extend(basenames)
        return autousenames

    def getfixtureclosure(
        self, fixturenames: Tuple[str, ...], parentnode, ignore_args: Sequence[str] = ()
    ) -> Tuple[Tuple[str, ...], List[str], Dict[str, Sequence[FixtureDef]]]:
        # collect the closure of all fixtures , starting with the given
        # fixturenames as the initial set.  As we have to visit all
        # factory definitions anyway, we also return an arg2fixturedefs
        # mapping so that the caller can reuse it and does not have
        # to re-discover fixturedefs again for each fixturename
        # (discovering matching fixtures for a given name/node is expensive)

        parentid = parentnode.nodeid
        fixturenames_closure = self._getautousenames(parentid)

        def merge(otherlist: Iterable[str]) -> None:
            for arg in otherlist:
                if arg not in fixturenames_closure:
                    fixturenames_closure.append(arg)

        merge(fixturenames)

        # at this point, fixturenames_closure contains what we call "initialnames",
        # which is a set of fixturenames the function immediately requests. We
        # need to return it as well, so save this.
        initialnames = tuple(fixturenames_closure)

        arg2fixturedefs = {}  # type: Dict[str, Sequence[FixtureDef]]
        lastlen = -1
        while lastlen != len(fixturenames_closure):
            lastlen = len(fixturenames_closure)
            for argname in fixturenames_closure:
                if argname in ignore_args:
                    continue
                if argname in arg2fixturedefs:
                    continue
                fixturedefs = self.getfixturedefs(argname, parentid)
                if fixturedefs:
                    arg2fixturedefs[argname] = fixturedefs
                    merge(fixturedefs[-1].argnames)

        def sort_by_scope(arg_name: str) -> int:
            try:
                fixturedefs = arg2fixturedefs[arg_name]
            except KeyError:
                return scopes.index("function")
            else:
                return fixturedefs[-1].scopenum

        fixturenames_closure.sort(key=sort_by_scope)
        return initialnames, fixturenames_closure, arg2fixturedefs

    def pytest_generate_tests(self, metafunc: "Metafunc") -> None:
        for argname in metafunc.fixturenames:
            faclist = metafunc._arg2fixturedefs.get(argname)
            if faclist:
                fixturedef = faclist[-1]
                if fixturedef.params is not None:
                    markers = list(metafunc.definition.iter_markers("parametrize"))
                    for parametrize_mark in markers:
                        if "argnames" in parametrize_mark.kwargs:
                            argnames = parametrize_mark.kwargs["argnames"]
                        else:
                            argnames = parametrize_mark.args[0]

                        if not isinstance(argnames, (tuple, list)):
                            argnames = [
                                x.strip() for x in argnames.split(",") if x.strip()
                            ]
                        if argname in argnames:
                            break
                    else:
                        metafunc.parametrize(
                            argname,
                            fixturedef.params,
                            indirect=True,
                            scope=fixturedef.scope,
                            ids=fixturedef.ids,
                        )
            else:
                continue  # will raise FixtureLookupError at setup time

    def pytest_collection_modifyitems(self, items: "List[nodes.Item]") -> None:
        # separate parametrized setups
        items[:] = reorder_items(items)

    def parsefactories(
        self, node_or_obj, nodeid=NOTSET, unittest: bool = False
    ) -> None:
        if nodeid is not NOTSET:
            holderobj = node_or_obj
        else:
            holderobj = node_or_obj.obj
            nodeid = node_or_obj.nodeid
        if holderobj in self._holderobjseen:
            return

        self._holderobjseen.add(holderobj)
        autousenames = []
        for name in dir(holderobj):
            # The attribute can be an arbitrary descriptor, so the attribute
            # access below can raise. safe_getatt() ignores such exceptions.
            obj = safe_getattr(holderobj, name, None)
            marker = getfixturemarker(obj)
            if not isinstance(marker, FixtureFunctionMarker):
                # magic globals  with __getattr__ might have got us a wrong
                # fixture attribute
                continue

            if marker.name:
                name = marker.name

            # during fixture definition we wrap the original fixture function
            # to issue a warning if called directly, so here we unwrap it in order to not emit the warning
            # when pytest itself calls the fixture function
            obj = get_real_method(obj, holderobj)

            fixture_def = FixtureDef(
                fixturemanager=self,
                baseid=nodeid,
                argname=name,
                func=obj,
                scope=marker.scope,
                params=marker.params,
                unittest=unittest,
                ids=marker.ids,
            )

            faclist = self._arg2fixturedefs.setdefault(name, [])
            if fixture_def.has_location:
                faclist.append(fixture_def)
            else:
                # fixturedefs with no location are at the front
                # so this inserts the current fixturedef after the
                # existing fixturedefs from external plugins but
                # before the fixturedefs provided in conftests.
                i = len([f for f in faclist if not f.has_location])
                faclist.insert(i, fixture_def)
            if marker.autouse:
                autousenames.append(name)

        if autousenames:
            self._nodeid_and_autousenames.append((nodeid or "", autousenames))

    def getfixturedefs(
        self, argname: str, nodeid: str
    ) -> Optional[Sequence[FixtureDef]]:
        """
        Gets a list of fixtures which are applicable to the given node id.

        :param str argname: name of the fixture to search for
        :param str nodeid: full node id of the requesting test.
        :return: list[FixtureDef]
        """
        try:
            fixturedefs = self._arg2fixturedefs[argname]
        except KeyError:
            return None
        return tuple(self._matchfactories(fixturedefs, nodeid))

    def _matchfactories(
        self, fixturedefs: Iterable[FixtureDef], nodeid: str
    ) -> Iterator[FixtureDef]:
        from _pytest import nodes

        for fixturedef in fixturedefs:
            if nodes.ischildnode(fixturedef.baseid, nodeid):
                yield fixturedef
