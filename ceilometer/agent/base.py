#
# Copyright 2013 Julien Danjou
# Copyright 2014 Red Hat, Inc
#
# Authors: Julien Danjou <julien@danjou.info>
#          Eoghan Glynn <eglynn@redhat.com>
#          Nejc Saje <nsaje@redhat.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections
import fnmatch
import itertools
import random

from oslo_config import cfg
from oslo_context import context
import six
from six.moves.urllib import parse as urlparse
from stevedore import extension

from ceilometer.agent import plugin_base
from ceilometer import coordination
from ceilometer.i18n import _
from ceilometer.openstack.common import log
from ceilometer.openstack.common import service as os_service
from ceilometer import pipeline as publish_pipeline
from ceilometer import utils

LOG = log.getLogger(__name__)

OPTS = [
    cfg.IntOpt('shuffle_time_before_polling_task',
               default=0,
               help='To reduce large requests at same time to Nova or other '
                    'components from different compute agents, shuffle '
                    'start time of polling task.'),
]

cfg.CONF.register_opts(OPTS)


class PollsterListForbidden(Exception):
    def __init__(self):
        msg = ('It is forbidden to use pollster-list option of polling agent '
               'in case of using coordination between multiple agents. Please '
               'use either multiple agents being coordinated or polling list '
               'option for one polling agent.')
        super(PollsterListForbidden, self).__init__(msg)


class Resources(object):
    def __init__(self, agent_manager):
        self.agent_manager = agent_manager
        self._resources = []
        self._discovery = []
        self.blacklist = []

    def setup(self, pipeline):
        self._resources = pipeline.resources
        self._discovery = pipeline.discovery

    def get(self, discovery_cache=None):
        source_discovery = (self.agent_manager.discover(self._discovery,
                                                        discovery_cache)
                            if self._discovery else [])
        static_resources = []
        if self._resources:
            static_resources_group = self.agent_manager.construct_group_id(
                utils.hash_of_set(self._resources))
            p_coord = self.agent_manager.partition_coordinator
            static_resources = p_coord.extract_my_subset(
                static_resources_group, self._resources)
        return static_resources + source_discovery

    @staticmethod
    def key(source_name, pollster):
        return '%s-%s' % (source_name, pollster.name)


class PollingTask(object):
    """Polling task for polling samples and inject into pipeline.

    A polling task can be invoked periodically or only once.
    """

    def __init__(self, agent_manager):
        self.manager = agent_manager

        # elements of the Cartesian product of sources X pollsters
        # with a common interval
        self.pollster_matches = collections.defaultdict(set)

        # per-sink publisher contexts associated with each source
        self.publishers = {}

        # we relate the static resources and per-source discovery to
        # each combination of pollster and matching source
        resource_factory = lambda: Resources(agent_manager)
        self.resources = collections.defaultdict(resource_factory)

    def add(self, pollster, pipeline):
        if pipeline.source.name not in self.publishers:
            publish_context = publish_pipeline.PublishContext(
                self.manager.context)
            self.publishers[pipeline.source.name] = publish_context
        self.publishers[pipeline.source.name].add_pipelines([pipeline])
        self.pollster_matches[pipeline.source.name].add(pollster)
        key = Resources.key(pipeline.source.name, pollster)
        self.resources[key].setup(pipeline)

    def poll_and_publish(self):
        """Polling sample and publish into pipeline."""
        cache = {}
        discovery_cache = {}
        for source_name in self.pollster_matches:
            with self.publishers[source_name] as publisher:
                for pollster in self.pollster_matches[source_name]:
                    LOG.info(_("Polling pollster %(poll)s in the context of "
                               "%(src)s"),
                             dict(poll=pollster.name, src=source_name))
                    pollster_resources = []
                    if pollster.obj.default_discovery:
                        pollster_resources = self.manager.discover(
                            [pollster.obj.default_discovery], discovery_cache)
                    key = Resources.key(source_name, pollster)
                    source_resources = list(
                        self.resources[key].get(discovery_cache))
                    candidate_res = (source_resources or
                                     pollster_resources)

                    # Exclude the failed resource from polling
                    black_res = self.resources[key].blacklist
                    polling_resources = [
                        x for x in candidate_res if x not in black_res]

                    # If no resources, skip for this pollster
                    if not polling_resources:
                        LOG.info(_("Skip polling pollster %s, no resources"
                                   " found"), pollster.name)
                        continue

                    try:
                        samples = list(pollster.obj.get_samples(
                            manager=self.manager,
                            cache=cache,
                            resources=polling_resources
                        ))
                        publisher(samples)
                    except plugin_base.PollsterPermanentError as err:
                        LOG.error(_(
                            'Prevent pollster %(name)s for '
                            'polling source %(source)s anymore!')
                            % ({'name': pollster.name, 'source': source_name}))
                        self.resources[key].blacklist.append(err.fail_res)
                    except Exception as err:
                        LOG.warning(_(
                            'Continue after error from %(name)s: %(error)s')
                            % ({'name': pollster.name, 'error': err}),
                            exc_info=True)


class AgentManager(os_service.Service):

    def __init__(self, namespaces, pollster_list, group_prefix=None):
        super(AgentManager, self).__init__()

        def _match(pollster):
            """Find out if pollster name matches to one of the list."""
            return any(fnmatch.fnmatch(pollster.name, pattern) for
                       pattern in pollster_list)

        # features of using coordination and pollster-list are exclusive, and
        # cannot be used at one moment to avoid both samples duplication and
        # samples being lost
        if pollster_list and cfg.CONF.coordination.backend_url:
            raise PollsterListForbidden()

        if type(namespaces) is not list:
            namespaces = [namespaces]

        # we'll have default ['compute', 'central'] here if no namespaces will
        # be passed
        extensions = (self._extensions('poll', namespace).extensions
                      for namespace in namespaces)
        if pollster_list:
            extensions = (itertools.ifilter(_match, exts)
                          for exts in extensions)

        self.extensions = list(itertools.chain(*list(extensions)))

        self.discovery_manager = self._extensions('discover')
        self.context = context.RequestContext('admin', 'admin', is_admin=True)
        self.partition_coordinator = coordination.PartitionCoordinator()

        # Compose coordination group prefix.
        # We'll use namespaces as the basement for this partitioning.
        namespace_prefix = '-'.join(sorted(namespaces))
        self.group_prefix = ('%s-%s' % (namespace_prefix, group_prefix)
                             if group_prefix else namespace_prefix)

    @staticmethod
    def _extensions(category, agent_ns=None):
        namespace = ('ceilometer.%s.%s' % (category, agent_ns) if agent_ns
                     else 'ceilometer.%s' % category)

        def _catch_extension_load_error(mgr, ep, exc):
            # Extension raising ExtensionLoadError can be ignored
            if isinstance(exc, plugin_base.ExtensionLoadError):
                LOG.error(_("Skip loading extension for %s") % ep.name)
                return
            raise exc

        return extension.ExtensionManager(
            namespace=namespace,
            invoke_on_load=True,
            on_load_failure_callback=_catch_extension_load_error,
        )

    def join_partitioning_groups(self):
        groups = set([self.construct_group_id(d.obj.group_id)
                      for d in self.discovery_manager])
        # let each set of statically-defined resources have its own group
        static_resource_groups = set([
            self.construct_group_id(utils.hash_of_set(p.resources))
            for p in self.pipeline_manager.pipelines
            if p.resources
        ])
        groups.update(static_resource_groups)
        for group in groups:
            self.partition_coordinator.join_group(group)

    def create_polling_task(self):
        """Create an initially empty polling task."""
        return PollingTask(self)

    def setup_polling_tasks(self):
        polling_tasks = {}
        for pipeline in self.pipeline_manager.pipelines:
            for pollster in self.extensions:
                if pipeline.support_meter(pollster.name):
                    polling_task = polling_tasks.get(pipeline.get_interval())
                    if not polling_task:
                        polling_task = self.create_polling_task()
                        polling_tasks[pipeline.get_interval()] = polling_task
                    polling_task.add(pollster, pipeline)

        return polling_tasks

    def construct_group_id(self, discovery_group_id):
        return ('%s-%s' % (self.group_prefix,
                           discovery_group_id)
                if discovery_group_id else None)

    def start(self):
        self.pipeline_manager = publish_pipeline.setup_pipeline()

        self.partition_coordinator.start()
        self.join_partitioning_groups()

        # allow time for coordination if necessary
        delay_start = self.partition_coordinator.is_active()

        # set shuffle time before polling task if necessary
        delay_polling_time = random.randint(
            0, cfg.CONF.shuffle_time_before_polling_task)

        for interval, task in six.iteritems(self.setup_polling_tasks()):
            delay_time = (interval + delay_polling_time if delay_start
                          else delay_polling_time)
            self.tg.add_timer(interval,
                              self.interval_task,
                              initial_delay=delay_time,
                              task=task)
        self.tg.add_timer(cfg.CONF.coordination.heartbeat,
                          self.partition_coordinator.heartbeat)

    @staticmethod
    def interval_task(task):
        task.poll_and_publish()

    @staticmethod
    def _parse_discoverer(url):
        s = urlparse.urlparse(url)
        return (s.scheme or s.path), (s.netloc + s.path if s.scheme else None)

    def _discoverer(self, name):
        for d in self.discovery_manager:
            if d.name == name:
                return d.obj
        return None

    def discover(self, discovery=None, discovery_cache=None):
        resources = []
        discovery = discovery or []
        for url in discovery:
            if discovery_cache is not None and url in discovery_cache:
                resources.extend(discovery_cache[url])
                continue
            name, param = self._parse_discoverer(url)
            discoverer = self._discoverer(name)
            if discoverer:
                try:
                    discovered = discoverer.discover(self, param)
                    partitioned = self.partition_coordinator.extract_my_subset(
                        self.construct_group_id(discoverer.group_id),
                        discovered)
                    resources.extend(partitioned)
                    if discovery_cache is not None:
                        discovery_cache[url] = partitioned
                except Exception as err:
                    LOG.exception(_('Unable to discover resources: %s') % err)
            else:
                LOG.warning(_('Unknown discovery extension: %s') % name)
        return resources
