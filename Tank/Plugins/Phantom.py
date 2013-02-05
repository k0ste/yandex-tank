''' Contains Phantom Plugin, Console widgets, result reader classes '''
from Tank.Plugins import ConsoleScreen
from Tank.Plugins.Aggregator import AggregatorPlugin, AggregateResultListener, \
    AbstractReader
from Tank.Plugins.Autostop import AutostopPlugin, AbstractCriteria
from Tank.Plugins.ConsoleOnline import ConsoleOnlinePlugin, AbstractInfoWidget
from Tank.Plugins.PhantomUtils import PhantomConfig
from tankcore import AbstractPlugin
import os
import socket
import subprocess
import sys
import tankcore
import time
import datetime

# TODO: 2 if instances_schedule enabled - pass to phantom the top count as instances limit
# FIXME: 3 there is no graceful way to interrupt the process in phout import mode 
class PhantomPlugin(AbstractPlugin, AggregateResultListener):
    '''
    Plugin for running phantom tool
    '''

    OPTION_CONFIG = "config"
    SECTION = PhantomConfig.SECTION
    
    def __init__(self, core):
        AbstractPlugin.__init__(self, core)
        self.config = None
        self.process = None

        self.predefined_phout = None
        self.phout_import_mode = False
        self.did_phout_import_try = False
        
        self.phantom_path = None
        self.eta_file = None
        self.processed_ammo_count = 0
        self.phantom_start_time = time.time()
        self.buffered_seconds = "2"

        self.phantom = None
        self.cached_info = None
                
    
    @staticmethod
    def get_key():
        return __file__
    
    
    def configure(self):       
        # plugin part
        self.config = self.get_option(self.OPTION_CONFIG, '')
        self.eta_file = self.get_option("eta_file", '')
        self.core.add_artifact_file(self.eta_file)        
        self.phantom_path = self.get_option("phantom_path", 'phantom')
        self.buffered_seconds = int(self.get_option("buffered_seconds", self.buffered_seconds))
        
        try:
            autostop = self.core.get_plugin_of_type(AutostopPlugin)
            autostop.add_criteria_class(UsedInstancesCriteria)
        except KeyError:
            self.log.debug("No autostop plugin found, not adding instances criteria")
            
        self.predefined_phout = self.get_option(PhantomConfig.OPTION_PHOUT, '')
        if not self.get_option(self.OPTION_CONFIG, '') and self.predefined_phout:
            self.phout_import_mode = True

        if not self.config and not self.phout_import_mode:
            self.phantom = PhantomConfig(self.core)
            self.phantom.read_config()
       

    def prepare_test(self):
        aggregator = None
        try:
            aggregator = self.core.get_plugin_of_type(AggregatorPlugin)
        except Exception, ex:
            self.log.warning("No aggregator found: %s", ex)

        if aggregator:
            aggregator.reader = PhantomReader(aggregator, self)
            aggregator.reader.buffered_seconds = self.buffered_seconds
            if self.phantom:
                self.phantom.set_timeout(aggregator.get_timeout())
            aggregator.add_result_listener(self)


        if not self.config and not self.phout_import_mode:
            aggregator.reader.phout_file = self.phantom.phout_file
                
            # generate config
            self.config = self.phantom.compose_config()
            args = [self.phantom_path, 'check', self.config]
            
            result = tankcore.execute(args, catch_out=True)
            retcode = result[0]
            if retcode:
                raise RuntimeError("Config check failed. Subprocess returned code %s" % retcode)
            if result[2]:
                raise RuntimeError("Subprocess returned message: %s" % result[2])
                    
        else:
            aggregator.reader.phout_file = self.predefined_phout
        
        try:
            console = self.core.get_plugin_of_type(ConsoleOnlinePlugin)
        except Exception, ex:
            self.log.debug("Console not found: %s", ex)
            console = None
            
        if console:
            if not self.phout_import_mode:    
                widget = PhantomProgressBarWidget(self)
                if self.eta_file:
                    widget.eta_file = self.eta_file
                console.add_info_widget(widget)
                aggregator = self.core.get_plugin_of_type(AggregatorPlugin)
                aggregator.add_result_listener(widget)

            widget = PhantomInfoWidget(self)
            console.add_info_widget(widget)
            aggregator = self.core.get_plugin_of_type(AggregatorPlugin)
            aggregator.add_result_listener(widget)

        
    def start_test(self):
        if not self.phout_import_mode:
            args = [self.phantom_path, 'run', self.config]
            self.log.debug("Starting %s with arguments: %s", self.phantom_path, args)
            self.phantom_start_time = time.time()
            self.process = subprocess.Popen(args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
        else:
            if not os.path.exists(self.predefined_phout):
                raise RuntimeError("Phout file not exists for import: %s" % self.predefined_phout)
            self.log.warn("Will import phout file instead of running phantom: %s", self.predefined_phout)
    

    def is_test_finished(self):
        if not self.phout_import_mode:
            tankcore.log_stdout_stderr(self.log, self.process.stdout, self.process.stderr, self.SECTION)
    
            retcode = self.process.poll()
            if retcode != None:
                self.log.info("Phantom done its work with exit code: %s", retcode)
                return abs(retcode)
            else:
                return -1
        else:
            if not self.processed_ammo_count or self.did_phout_import_try != self.processed_ammo_count:
                self.did_phout_import_try = self.processed_ammo_count
                return -1
            else:
                return 0
    
    
    def end_test(self, retcode):
        if self.process and self.process.poll() == None:
            self.log.warn("Terminating phantom process with PID %s", self.process.pid)
            self.process.terminate()
        else:
            self.log.debug("Seems phantom finished OK")
        return retcode

            
    def post_process(self, retcode):
        if not retcode:
            info = self.get_info()
            if info and info.ammo_count != self.processed_ammo_count:
                self.log.warning("Planned ammo count %s differs from processed %s", info.ammo_count, self.processed_ammo_count)
        return retcode


    def aggregate_second(self, second_aggregate_data):
        self.processed_ammo_count += second_aggregate_data.overall.RPS
        self.log.debug("Processed ammo count: %s/", self.processed_ammo_count)
        
        
    def get_info(self):
        ''' returns info object '''
        if not self.cached_info:
            if not self.phantom:
                return None
            self.cached_info = self.phantom.get_info()
        return self.cached_info
            

class PhantomProgressBarWidget(AbstractInfoWidget, AggregateResultListener):
    '''
    Widget that displays progressbar
    '''
    def get_index(self):
        return 0

    def __init__(self, sender):
        AbstractInfoWidget.__init__(self)
        self.krutilka = ConsoleScreen.krutilka()
        self.owner = sender 
        self.ammo_progress = 0
        self.eta_file = None
        
        info = self.owner.get_info()
        if info:
            self.ammo_count = int(info.ammo_count)
            self.test_duration = int(info.duration)
        else:
            self.ammo_count = 0
            self.test_duration = 0

    
    def render(self, screen):
        res = ""

        dur_seconds = int(time.time()) - int(self.owner.phantom_start_time)

        eta_time = 'N/A' 
        eta_secs = -1
        progress = 0
        color_bg = screen.markup.BG_CYAN
        color_fg = screen.markup.CYAN
        if self.test_duration and self.test_duration >= dur_seconds:
            color_bg = screen.markup.BG_GREEN
            color_fg = screen.markup.GREEN
            eta_secs = self.test_duration - dur_seconds
            eta_time = datetime.timedelta(seconds=eta_secs)
            progress = float(dur_seconds) / self.test_duration
        elif self.ammo_progress:
            left_part = self.ammo_count - self.ammo_progress
            eta_secs = int(float(dur_seconds) / float(self.ammo_progress) * float(left_part))
            eta_time = datetime.timedelta(seconds=eta_secs)
            progress = float(self.ammo_progress) / float(self.ammo_count)

        if self.eta_file:
            handle = open(self.eta_file, 'w')
            handle.write(str(eta_secs))
            handle.close()

        perc = float(int(1000 * progress)) / 10
        str_perc = str(perc) + "%"
        
        pb_width = screen.right_panel_width - 1 - len(str_perc)
        
        progress_chars = '=' * (int(pb_width * progress) - 1)
        progress_chars += self.krutilka.next()
        
        res += color_bg + progress_chars + screen.markup.RESET + color_fg + '~' * (pb_width - int(pb_width * progress)) + screen.markup.RESET + ' '
        res += str_perc + "\n"

        eta = 'ETA: %s' % eta_time
        dur = 'Duration: %s' % str(datetime.timedelta(seconds=dur_seconds))
        spaces = ' ' * (screen.right_panel_width - len(eta) - len(dur) - 1)
        res += dur + ' ' + spaces + eta

        return res

    def aggregate_second(self, second_aggregate_data):
        self.ammo_progress += second_aggregate_data.overall.RPS


class PhantomInfoWidget(AbstractInfoWidget, AggregateResultListener):
    '''
    Widget with information about current run state
    '''
    def get_index(self):
        return 2

    def __init__(self, sender):
        AbstractInfoWidget.__init__(self)
        self.owner = sender 
        self.instances = 0
        self.planned = 0
        self.RPS = 0    
        self.selfload = 0
        self.time_lag = 0
        self.planned_rps_duration = 0

        info = self.owner.get_info()
        if info:
            self.instances_limit = int(info.instances)
            self.ammo_count = int(info.ammo_count)
        else:
            self.instances_limit = 1
            self.ammo_count = 1

    def render(self, screen):
        res = ''
        info = self.owner.get_info()
        if self.owner.phantom:
            template = "Hosts: %s => %s:%s\n Ammo: %s\nCount: %s\n Load: %s"
            data = (socket.gethostname(), info.address, info.port, os.path.basename(info.ammo_file), self.ammo_count, ' '.join(info.rps_schedule))
            res = template % data
            
            res += "\n\n"
        
        res += "Active instances: "
        if float(self.instances) / self.instances_limit > 0.8:
            res += screen.markup.RED + str(self.instances) + screen.markup.RESET
        elif float(self.instances) / self.instances_limit > 0.5:
            res += screen.markup.YELLOW + str(self.instances) + screen.markup.RESET
        else:
            res += str(self.instances)
            
        res += "\nPlanned requests: %s for %s\nActual responses: " % (self.planned, datetime.timedelta(seconds=self.planned_rps_duration))
        if not self.planned == self.RPS:
            res += screen.markup.YELLOW + str(self.RPS) + screen.markup.RESET
        else:
            res += str(self.RPS)
                
        res += "\n        Accuracy: "
        if self.selfload < 80:
            res += screen.markup.RED + ('%.2f' % self.selfload) + screen.markup.RESET
        elif self.selfload < 95:
            res += screen.markup.YELLOW + ('%.2f' % self.selfload) + screen.markup.RESET
        else:
            res += ('%.2f' % self.selfload)

        res += "%\n        Time lag: "        
        if self.time_lag > self.owner.buffered_seconds * 5:
            res += screen.markup.RED + str(datetime.timedelta(seconds=self.time_lag)) + screen.markup.RESET
        elif self.time_lag > self.owner.buffered_seconds:
            res += screen.markup.YELLOW + str(datetime.timedelta(seconds=self.time_lag)) + screen.markup.RESET
        else:
            res += str(datetime.timedelta(seconds=self.time_lag))
                
        return res


    def aggregate_second(self, second_aggregate_data):
        self.instances = second_aggregate_data.overall.active_threads
        if self.planned == second_aggregate_data.overall.planned_requests:
            self.planned_rps_duration += 1
        else:
            self.planned = second_aggregate_data.overall.planned_requests
            self.planned_rps_duration = 1
        
        self.RPS = second_aggregate_data.overall.RPS
        self.selfload = second_aggregate_data.overall.selfload
        self.time_lag = int(time.time() - time.mktime(second_aggregate_data.time.timetuple()))
    
    
class PhantomReader(AbstractReader):
    '''
    Adapter to read phout files
    '''

    def __init__(self, owner, phantom):
        AbstractReader.__init__(self, owner)
        self.phantom = phantom
        self.phout_file = None
        self.phout = None
        self.stat = None
        self.stat_data = {}
        self.pending_datetime = None
        self.steps = []
        self.first_request_time = sys.maxint
        self.partial_buffer = ''
        self.pending_second_data_queue = []
        self.last_sample_time = 0
        self.read_lines_count = 0
        self.buffered_seconds = 3
  
    def check_open_files(self):
        info = self.phantom.get_info()
        if not self.phout and os.path.exists(self.phout_file):
            self.log.debug("Opening phout file: %s", self.phout_file)
            self.phout = open(self.phout_file, 'r')
            if info:
                self.steps = info.steps 
        
        if not self.stat and info and os.path.exists(info.stat_log):
            self.log.debug("Opening stat file: %s", self.phantom.phantom.stat_log)
            self.stat = open(self.phantom.phantom.stat_log, 'r')

    def close_files(self):
        if self.stat:
            self.stat.close()
            
        if self.phout:
            self.phout.close()

    def get_next_sample(self, force):
        if self.stat: 
            self.__read_stat_data()
        return self.__read_phout_data(force)

    def __read_stat_data(self):
        '''
        Read active instances info
        '''
        stat = self.stat.readlines()
        for line in stat:
            if line.startswith('time\t'):
                date_str = line[len('time:\t') - 1:].strip()[:-5].strip()
                date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                self.pending_datetime = int(time.mktime(date_obj.timetuple()))
                self.stat_data[self.pending_datetime] = 0
            if line.startswith('tasks\t'):
                if not self.pending_datetime:
                    raise RuntimeError("Can't have tasks info without timestamp")
                
                self.stat_data[self.pending_datetime] += int(line[len('tasks\t'):])
                self.log.debug("Active instances: %s=>%s", self.pending_datetime, self.stat_data[self.pending_datetime])


    def __read_phout_data(self, force):
        '''
        Read phantom results
        '''
        if self.phout and len(self.data_queue) < self.buffered_seconds * 2:
            phout = self.phout.readlines(5 * 1024 * 1024)
        else:
            phout = []
    
        self.log.debug("About to process %s phout lines", len(phout))
        for line in phout:
            line = self.partial_buffer + line
            self.partial_buffer = ''
            if line[-1] != "\n":
                self.log.debug("Not complete line, buffering it: %s", line)
                self.partial_buffer = line
                continue
            line = line.strip()
            if not line:
                self.log.warning("Empty phout line")
                continue
            # 1346949510.514        74420    66    78    65409    8867    74201    18    15662    0    200
            # self.log.debug("Phout line: %s", line)
            self.read_lines_count += 1
            data = line.split("\t")
            if len(data) != 12:
                self.log.warning("Wrong phout line, skipped: %s", line)
                continue
            cur_time = int(float(data[0]) + float(data[2]) / 1000000)
            # self.log.info("%s => %s", data[0], cur_time)
            try:
                active = self.stat_data[cur_time]
            except KeyError:
                # self.log.debug("No tasks info for second yet: %s", cur_time)
                active = 0

            if not cur_time in self.data_buffer.keys():
                self.first_request_time = min(self.first_request_time, int(float(data[0])))
                if self.data_queue and self.data_queue[-1] >= cur_time:
                    self.log.warning("Aggregator data dates must be sequential: %s vs %s" % (cur_time, self.data_queue[-1]))
                    cur_time = self.data_queue[-1]
                else:
                    self.data_queue.append(cur_time)
                    self.data_buffer[cur_time] = []
            #        marker, threads, overallRT, httpCode, netCode
            data_item = [data[1], active, int(data[2]) / 1000, data[11], data[10]]
            # bytes:     sent    received
            data_item += [int(data[8]), int(data[9])]
            #        connect    send    latency    receive
            data_item += [int(data[3]) / 1000, int(data[4]) / 1000, int(data[5]) / 1000, int(data[6]) / 1000]
            #        accuracy
            data_item += [(float(data[7]) + 1) / (int(data[2]) + 1)]
            self.data_buffer[cur_time].append(data_item)

        self.log.debug("Read lines: %s", self.read_lines_count)                    
        self.log.debug("Seconds queue: %s", self.data_queue)
        self.log.debug("Seconds buffer (up to %s): %s", self.buffered_seconds, self.data_buffer.keys())        
        if len(self.data_queue) > self.buffered_seconds:
            self.log.debug("Should send!")
            return self.pop_second()
        
        if force and self.data_queue:
            return self.pop_second()
        else:
            self.log.debug("No queue data!")
            return None 


    def pop_second(self):
        parsed_sec = AbstractReader.pop_second(self)
        if parsed_sec:
            self.pending_second_data_queue.append(parsed_sec)
        else:
            self.log.debug("No new seconds present")   
            
        if not self.pending_second_data_queue:
            self.log.debug("pending_second_data_queue empty")
            return None
        else:
            self.log.debug("pending_second_data_queue: %s", self.pending_second_data_queue)


        next_time = int(time.mktime(self.pending_second_data_queue[0].time.timetuple()))
            
        if self.last_sample_time and (next_time - self.last_sample_time) > 1:
            self.last_sample_time += 1
            self.log.debug("Adding phantom zero sample: %s", self.last_sample_time)
            res = self.get_zero_sample(datetime.datetime.fromtimestamp(self.last_sample_time))
        else:
            res = self.pending_second_data_queue.pop(0)
        
        self.last_sample_time = int(time.mktime(res.time.timetuple()))
        res.overall.planned_requests = self.__get_expected_rps()
        self.log.debug("Pop result: %s", res)
        return res
    

    def __get_expected_rps(self):
        '''
        Mark second with expected rps
        '''
        while self.steps and self.steps[0][1] < 1:
            self.steps.pop(0)
        
        if not self.steps:
            return 0
        else:
            self.steps[0][1] -= 1
            return self.steps[0][0]
            
     
class UsedInstancesCriteria(AbstractCriteria):
    '''
    Autostop criteria, based on active instances count
    '''
    RC_INST = 24
    
    @staticmethod
    def get_type_string():
        return 'instances'

    def __init__(self, autostop, param_str):
        AbstractCriteria.__init__(self)
        self.seconds_count = 0
        self.autostop = autostop
        self.threads_limit = 1

        level_str = param_str.split(',')[0].strip()
        if level_str[-1:] == '%':
            self.level = float(level_str[:-1]) / 100
            self.is_relative = True
        else:
            self.level = int(level_str)
            self.is_relative = False
        self.seconds_limit = tankcore.expand_to_seconds(param_str.split(',')[1])
        
        try:
            phantom = autostop.core.get_plugin_of_type(PhantomPlugin)
            info = phantom.get_info()
            if info:
                self.threads_limit = info.instances
            if not self.threads_limit:
                raise ValueError("Cannot create 'instances' criteria with zero instances limit")
        except KeyError:
            self.log.warning("No phantom module, 'instances' autostop disabled")


    def notify(self, aggregate_second):
        threads = aggregate_second.overall.active_threads
        if self.is_relative:
            threads = float(threads) / self.threads_limit
        if threads > self.level:
            if not self.seconds_count:
                self.cause_second = aggregate_second
            
            self.log.debug(self.explain())
            
            self.seconds_count += 1
            self.autostop.add_counting(self)
            if self.seconds_count >= self.seconds_limit:
                return True
        else:
            self.seconds_count = 0
            
        return False

    def get_rc(self):
        return self.RC_INST

    def get_level_str(self):
        '''
        String value for instances level
        '''
        if self.is_relative:
            level_str = str(100 * self.level) + "%"
        else:
            level_str = self.level
        return level_str

    def explain(self):
        items = (self.get_level_str(), self.seconds_count, self.cause_second.time)
        return "Testing threads (instances) utilization higher than %s for %ss, since %s" % items                 

    def widget_explain(self):
        items = (self.get_level_str(), self.seconds_count, self.seconds_limit)
        return ("Instances >%s for %s/%ss" % items, float(self.seconds_count) / self.seconds_limit)


# ==================================================================================================
