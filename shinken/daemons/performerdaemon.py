#!/usr/bin/python

# -*- coding: utf-8 -*-

# Copyright (C) 2009-2012:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

import os
import time
import traceback
import sys

from multiprocessing import active_children
from Queue import Empty

from shinken.satellite import Satellite
from shinken.property import PathProp, IntegerProp
from shinken.log import logger
from shinken.external_command import ExternalCommand, ExternalCommandManager
from shinken.http_client import HTTPClient, HTTPExceptions
from shinken.daemon import Daemon, Interface

class IStats(Interface):
    """ 
    Interface for various stats about broker activity
    """

    doc = '''Get raw stats from the daemon:
  * command_buffer_size: external command buffer size
'''
    def get_raw_stats(self):
        app = self.app
        res = {'command_buffer_size': len(app.external_commands)}
        return res
    get_raw_stats.doc = doc

class IPerf(Interface):
    """ 
    Interface for send broks to the performer daemon
    """

    doc = '''Get brok from the daemon:
  * command_buffer_size: external command buffer size
'''
    def push_brok(self,b):
        print b
        logger.debug("[Performer Daemon]: the brok is %s " % b)
        return


# Our main APP class
class Performer(Satellite):

    properties = Satellite.properties.copy()
    properties.update({
        'pidfile':   PathProp(default='performerd.pid'),
        'port':      IntegerProp(default='7774'), #7774 = Performer's port
        'local_log': PathProp(default='performerd.log'),
    })

    def __init__(self, config_file, is_daemon, do_replace, debug, debug_file):

        super(Performer, self).__init__('performer', config_file, is_daemon, do_replace, debug, debug_file)

        # Our arbiters
        self.arbiters = {}

        # Our pollers and reactionners
        self.pollers = {}
        self.reactionners = {}

        # Modules are load one time
        self.have_modules = False

        # Can have a queue of external_commands give by modules
        # will be taken by arbiter to process
        self.external_commands = []
        # and the unprocessed one, a buffer
        self.unprocessed_external_commands = []

        # All broks to manage
        self.broks = []  # broks to manage
        # broks raised this turn and that need to be put in self.broks
        self.broks_internal_raised = []

        self.host_assoc = {}
        self.direct_routing = False

        self.istats = IStats(self)
        self.iperf =  IPerf(self)

    # Schedulers have some queues. We can simplify call by adding
    # elements into the proper queue just by looking at their type
    # Brok -> self.broks
    # TODO: better tag ID?
    # External commands -> self.external_commands
    def add(self, elt):
        cls_type = elt.__class__.my_type
        if cls_type == 'brok':
            # For brok, we TAG brok with our instance_id
            elt.instance_id = 0
            self.broks_internal_raised.append(elt)
            return
        elif cls_type == 'externalcommand':
            logger.debug("Enqueuing an external command: %s" % str(ExternalCommand.__dict__))
            self.unprocessed_external_commands.append(elt)


    def push_host_names(self, sched_id, hnames):
        for h in hnames:
            self.host_assoc[h] = sched_id


    def get_sched_from_hname(self, hname):
        i = self.host_assoc.get(hname, None)
        e = self.schedulers.get(i, None)
        return e


    # Get a brok. Our role is to put it in the modules
    # THEY MUST DO NOT CHANGE data of b!!!
    # REF: doc/performer-modules.png (4-5)
    def manage_brok(self, b):
        to_del = []
        # Call all modules if they catch the call
        for mod in self.modules_manager.get_internal_instances():
            try:
                mod.manage_brok(b)
            except Exception, exp:
                logger.warning("The mod %s raise an exception: %s, I kill it" % (mod.get_name(), str(exp)))
                logger.warning("Exception type: %s" % type(exp))
                logger.warning("Back trace of this kill: %s" % (traceback.format_exc()))
                to_del.append(mod)
        # Now remove mod that raise an exception
        self.modules_manager.clear_instances(to_del)


    # Get 'objects' from external modules
    # from now nobody use it, but it can be useful
    # for a module like livestatus to raise external
    # commands for example
    def get_objects_from_from_queues(self):
        for f in self.modules_manager.get_external_from_queues():
            full_queue = True
            while full_queue:
                try:
                    o = f.get(block=False)
                    self.add(o)
                except Empty:
                    full_queue = False


    def do_stop(self):
        act = active_children()
        for a in act:
            a.terminate()
            a.join(1)
        super(Performer, self).do_stop()


    def setup_new_conf(self):
        conf = self.new_conf
        self.new_conf = None
        self.cur_conf = conf
        # Got our name from the globals
        if 'performer_name' in conf['global']:
            name = conf['global']['performer_name']
        else:
            name = 'Unnamed performer'
        self.name = name
        logger.load_obj(self, name)
        self.direct_routing = conf['global']['direct_routing']

        g_conf = conf['global']

        # If we've got something in the schedulers, we do not want it anymore
        for sched_id in conf['schedulers']:

            already_got = False

            # We can already got this conf id, but with another address
            if sched_id in self.schedulers:
                new_addr = conf['schedulers'][sched_id]['address']
                old_addr = self.schedulers[sched_id]['address']
                new_port = conf['schedulers'][sched_id]['port']
                old_port = self.schedulers[sched_id]['port']
                # Should got all the same to be ok :)
                if new_addr == old_addr and new_port == old_port:
                    already_got = True

            if already_got:
                logger.info("[%s] We already got the conf %d (%s)" % (self.name, sched_id, conf['schedulers'][sched_id]['name']))
                wait_homerun = self.schedulers[sched_id]['wait_homerun']
                actions = self.schedulers[sched_id]['actions']
                external_commands = self.schedulers[sched_id]['external_commands']
                con = self.schedulers[sched_id]['con']

            s = conf['schedulers'][sched_id]
            self.schedulers[sched_id] = s

            if s['name'] in g_conf['satellitemap']:
                s.update(g_conf['satellitemap'][s['name']])
            uri = 'http://%s:%s/' % (s['address'], s['port'])

            self.schedulers[sched_id]['uri'] = uri
            if already_got:
                self.schedulers[sched_id]['wait_homerun'] = wait_homerun
                self.schedulers[sched_id]['actions'] = actions
                self.schedulers[sched_id]['external_commands'] = external_commands
                self.schedulers[sched_id]['con'] = con
            else:
                self.schedulers[sched_id]['wait_homerun'] = {}
                self.schedulers[sched_id]['actions'] = {}
                self.schedulers[sched_id]['external_commands'] = []
                self.schedulers[sched_id]['con'] = None
            self.schedulers[sched_id]['running_id'] = 0
            self.schedulers[sched_id]['active'] = s['active']

            # Do not connect if we are a passive satellite
            if self.direct_routing and not already_got:
                # And then we connect to it :)
                self.pynag_con_init(sched_id)



        logger.debug("[%s] Sending us configuration %s" % (self.name, conf))

        if not self.have_modules:
            self.modules = mods = conf['global']['modules']
            self.have_modules = True
            logger.info("We received modules %s " % mods)
        # Set our giving timezone from arbiter
        use_timezone = conf['global']['use_timezone']
        if use_timezone != 'NOTSET':
            logger.info("Setting our timezone to %s" % use_timezone)
            os.environ['TZ'] = use_timezone
            time.tzset()


        # Now create the external commander. It's just here to dispatch
        # the commands to schedulers
        #e = ExternalCommandManager(None, 'performer')
        #e.load_performer(self)
        #self.external_command = e


    def do_loop_turn(self):
        sys.stdout.write(".")
        sys.stdout.flush()

        # Begin to clean modules
        self.check_and_del_zombie_modules()

        # Now we check if arbiter speak to us in the pyro_daemon.
        # If so, we listen for it
        # When it push us conf, we reinit connections
        self.watch_for_new_conf(0.0)
        if self.new_conf:
            self.setup_new_conf()

        start = time.time()

        # Maybe external modules raised 'objects'
        # we should get them
        self.get_objects_from_from_queues()

        # Maybe we do not have something to do, so we wait a little
        if len(self.broks) == 0:
            # print "watch new conf 1: begin", len(self.broks)
            self.watch_for_new_conf(1.0)
            # print "get enw broks watch new conf 1: end", len(self.broks)


    #  Main function, will loop forever
    def main(self):
        try:
            self.load_config_file()
            # Look if we are enabled or not. If ok, start the daemon mode
            self.look_for_early_exit()

            for line in self.get_header():
                logger.info(line)

            logger.info("[Performer] Using working directory: %s" % os.path.abspath(self.workdir))

            self.do_daemon_init_and_start()
            
            self.load_modules_manager()
            
            self.uri3 = self.http_daemon.register(self.iperf)
            self.uri2 = self.http_daemon.register(self.interface)#, "ForArbiter")
            self.uri3 = self.http_daemon.register(self.istats)

            
            #  We wait for initial conf
            self.wait_for_initial_conf()
            if not self.new_conf:
                return

            self.setup_new_conf()

            self.modules_manager.set_modules(self.modules)
            self.do_load_modules()
            # and start external modules too
            self.modules_manager.start_external_instances()

            # Do the modules part, we have our modules in self.modules
            # REF: doc/performer-modules.png (1)


            # Now the main loop
            self.do_mainloop()

        except Exception, exp:
            logger.critical("I got an unrecoverable error. I have to exit")
            logger.critical("You can log a bug ticket at https://github.com/naparuba/shinken/issues/new to get help")
            logger.critical("Back trace of it: %s" % (traceback.format_exc()))
            raise
