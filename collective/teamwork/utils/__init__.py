import itertools
import logging
import sys

from zope.component.hooks import getSite
from zope.globalrequest import getRequest
from zope.interface import alsoProvides, implements
from zope.interface.interfaces import IInterface
from zope.publisher.browser import setDefaultSkin
from z3c.form.interfaces import IFormLayer
from Acquisition import aq_base
from Products.CMFCore.utils import getToolByName
from Products.statusmessages.interfaces import IStatusMessage
from ZPublisher.HTTPResponse import HTTPResponse
from ZPublisher.HTTPRequest import HTTPRequest

from collective.teamwork.interfaces import IWorkspaceFinder
from collective.teamwork.interfaces import IProjectContext, IWorkspaceContext
from collective.teamwork.interfaces import ITeamworkProductLayer
from collective.teamwork.interfaces import APP_LOG


def make_request():
    """
    make request suitable for browser views and Zope2 security.
    """
    response = HTTPResponse(stdout=sys.stdout)
    request = HTTPRequest(
        sys.stdin,
        {
            'SERVER_NAME': 'localhost',
            'SERVER_PORT': '80',
            'REQUEST_METHOD': 'GET',
        },
        response,
        )
    setDefaultSkin(request)
    alsoProvides(request, IFormLayer)  # suitable for testing z3c.form views
    alsoProvides(request, ITeamworkProductLayer)  # product layer
    return request


def request_for(context):
    r = getattr(context, 'REQUEST', None)
    if isinstance(r, str) or r is None:
        return make_request()  # could not acquire REQUEST
    return r


def group_workspace(groupname):
    portal = getSite()
    r = portal.portal_catalog.unrestrictedSearchResults(
        {'pas_groups': groupname}
        )
    if not r:
        return None
    return r[0]._unrestrictedGetObject()


def _all_the_things(context, portal_type=None, iface=None):
    if not (iface or portal_type):
        raise ValueError('must provide either portal_type or iface')
    if IInterface.providedBy(iface):
        iface = iface.__identifier__
    site = getSite()
    query = {}
    if portal_type:
        query['portal_type'] = portal_type
    if iface:
        query['object_provides'] = iface
    if context is not site:
        query.update({'path': '/'.join(context.getPhysicalPath())})
    r = site.portal_catalog.unrestrictedSearchResults(query)
    _all_but_context = lambda o: aq_base(o) is not aq_base(context)
    return filter(_all_but_context, [b._unrestrictedGetObject() for b in r])


def get_projects(site=None):
    """
    Return all projects in site, found via catalog query.
    """
    site = site if site is not None else getSite()
    return _all_the_things(site, iface=IProjectContext)


def get_workspaces(context=None):
    """
    Return workspaces within context, if provided, or within
    the site if no context is provided.
    """
    context = context if context is not None else getSite()
    _sortkey = lambda o: len(o.getPhysicalPath())
    return sorted(
        _all_the_things(context, iface=IWorkspaceContext),
        key=_sortkey,
        )


def find_parents(context, findone=False, start_depth=2, **kwargs):
    typename = kwargs.get('typename', None)
    iface = kwargs.get('iface', None)
    if IInterface.providedBy(iface):
        iface = iface.__identifier__
    if findone and typename is None and iface is None:
        parent = getattr(context, '__parent__', None)
        if parent:
            return parent   # immediate parent of context
    result = []
    catalog = getToolByName(context, 'portal_catalog')
    path = context.getPhysicalPath()
    subpaths = reversed(
        [path[0:i] for i in range(len(path) + 1)][start_depth:]
        )
    for subpath in subpaths:
        query = {
            'path': {
                'query': '/'.join(subpath),
                'depth': 0,
                },
            }
        if typename is not None:
            query['portal_type'] = typename
        if iface is not None:
            query['object_provides'] = iface
        brains = catalog.unrestrictedSearchResults(query)
        if not brains:
            continue
        else:
            item = brains[0]._unrestrictedGetObject()
            if aq_base(item) is aq_base(context):
                continue  # don't return or append the context itself!
            if findone:
                return item
            result.append(item)
    if findone:
        return None     # never found one
    return result


def find_parent(context, **kwargs):
    return find_parents(context, findone=True, **kwargs)


def project_for(context):
    if IProjectContext.providedBy(context):
        return context
    return find_parent(context, iface=IProjectContext)


def workspace_for(context):
    if IWorkspaceContext.providedBy(context):
        return context
    return find_parent(context, iface=IWorkspaceContext, start_depth=3)


def parent_workspaces(context):
    workspace = workspace_for(context)
    if workspace is None:
        return []
    result = [workspace]
    parent = workspace.__parent__
    return list(itertools.chain(parent_workspaces(parent), result))


class WorkspaceUtilityView(object):
    """
    Workspace utility view: view or adapter for content context in
    a Plone site to get workspace or project context.
    """

    implements(IWorkspaceFinder)

    def __init__(self, context, request=None):
        self.context = context
        self.request = request

    def __call__(self, *args, **kwargs):
        content = "Workspace utility view"
        response = self.request.response
        response.setHeader('Content-type', 'text/plain')
        response.setHeader('Content-Length', len(content))
        return content

    def workspace(self):
        """get most immediate workspace containing or None"""
        return workspace_for(self.context)        # may be None

    def project(self):
        """get project containing or None"""
        return project_for(self.context)     # may be None


def session_id(context, request=None):
    request = request or getRequest()
    if request is None:
        return id(context)
    return id(request)


def authenticated_user(context):
    mtool = getToolByName(context, 'portal_membership')
    return mtool.getAuthenticatedMember().getUserName()


def log(message, context, request=None, prefix='', level=logging.INFO):
    if isinstance(message, unicode):
        message = message.encode('utf-8')
    session = session_id(context, request)
    prefix = prefix or 'WorkspaceMembership (%s)' % session
    user = authenticated_user(context)
    siteid = getSite().getId()
    path = '/'.join(context.getPhysicalPath())
    message = '%s: [%s] %s (%s) -- %s' % (
        prefix,
        siteid,
        path,
        user,
        message,
        )
    APP_LOG.log(level, message)


def log_status(message, context, request=None, prefix='', level=logging.INFO):
    request = request or getRequest()
    log(message, context, request, prefix, level)
    if request is not None:
        status_type = {
            logging.INFO: 'info',
            logging.WARNING: 'warn',
            logging.ERROR: 'error',
            }.get(level, 'info')
        if prefix:
            message = '%s %s' % (prefix, message)
        status = IStatusMessage(request)
        status.addStatusMessage(message, status_type)

