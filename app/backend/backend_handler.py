import webapp2
from google.appengine.api import background_thread, urlfetch, modules
from agents.base_agent import AgentRuntimeException
from utils import utils
import pickle
import urllib
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..',  'lib', 'amqp'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..',  'lib', 'kombu'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib', 'celery'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib', 'billiard'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib', 'anyjson'))
import time
import logging
from threading import Timer
import datetime

from google.appengine.ext import db
from kombu import Queue, Exchange

from common.config import AgentTypes
import common.helper as helper

__author__ = 'mengyuan, dev'
__email__ = 'gmy.melissa@gmail.com, dnath@cs.ucsb.edu'

BACKEND_NAME = 'backendthread'
BACKEND_URL = 'http://%s' % modules.get_hostname(BACKEND_NAME)

BACKEND_START = BACKEND_URL + '/_ah/start'
BACKEND_BACKGROUND = BACKEND_URL + '/_ah/background'
BACKEND_SYN_R_URL = BACKEND_URL + '/backend/synchronizedb'
BACKEND_MANAGER_R_URL = BACKEND_URL + '/backend/manager'
BACKEND_QUEUE_R_URL = 'http://%s' % modules.get_hostname('backendqueue') + '/backend/queue'

INS_TYPES_EC2 = ["t1.micro", "m1.small", "m3.medium", "m3.large", "c3.large", "c3.xlarge"]


class VMStateSyn(db.Model):
    last_syn = db.DateTimeProperty()


class VMStateModel(db.Model):
    IDS = 'ids'
    EC2_ACCESS_KEY = 'EC2_ACCESS_KEY'
    EC2_SECRET_KEY = 'EC2_SECRET_KEY'

    STATE_CREATING = 'creating'
    STATE_PENDING = 'pending'
    STATE_RUNNING = 'running'
    STATE_FAILED = 'failed'
    STATE_TERMINATED = 'terminated'

    DESCRI_FAIL_TO_RUN = 'fail to run the instance'
    DESCRI_TIMEOUT_ON_SSH = 'timeout to connect instance via ssh'
    DESCRI_FAIL_TO_COFIGURE_CELERY = 'fail to celery on the instance'
    DESCRI_NOT_FOUND = 'not find the instance in cloud infrastructure'
    DESCRI_SUCCESS = 'success'

    infra = db.StringProperty()
    access_key = db.StringProperty()
    secret_key = db.StringProperty()
    ins_type = db.StringProperty()
    res_id = db.StringProperty()
    ins_id = db.StringProperty()
    pub_ip = db.StringProperty()
    pri_ip = db.StringProperty()
    local_key = db.StringProperty()
    choices = set([STATE_CREATING, STATE_PENDING, STATE_RUNNING, STATE_FAILED, STATE_TERMINATED])
    state = db.StringProperty(required=True,
                              choices=choices)
    description = db.StringProperty()
    created = db.DateTimeProperty(auto_now_add=True)

    @staticmethod
    def get_all(params):
        '''
        get the information of all vms that are not terminated
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
        Return
            a dictionary of vms info
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return None

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('state !=', VMStateModel.STATE_TERMINATED)
            all_vms = []
            for e in entities:
                dict = {
                    "ins_id": e.ins_id,
                    "instance_type": e.ins_type,
                    "state": e.state,
                    "discription": e.description
                }
                all_vms.append(dict)
            return all_vms
        except Exception as e:
            logging.error("Error in getting all vms from db! {0}".format(e))
            return None

    @staticmethod
    def get_instance_type(params, ins_id):
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return None

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('ins_id ==', ins_id)

            e = entities.get()
            return e.ins_type

        except Exception as e:
            logging.error("Error in getting the instance type of instance {0} from db! {1}".format(ins_id, e))
            return None

    @staticmethod
    def get_running_instance_type(params):
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return None

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('state ==', VMStateModel.STATE_RUNNING)

            types = []
            for e in entities:
                types.append(e.ins_type)

            return list(set(types))

        except Exception as e:
            logging.error("Error in getting all running instance types from db! {0}".format(e))
            return None

    @staticmethod
    def terminate_not_active(params):
        '''
        update all vms that are 'failed' in the last launch to 'terminated'.
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('state =', VMStateModel.STATE_FAILED)

            for e in entities:
                e.state = VMStateModel.STATE_TERMINATED
                e.put()

        except Exception as e:
            logging.error("Error in updating non-active vms to terminated in db! {0}".format(e))

    @staticmethod
    def terminate_all(params):
        '''
        update the state of all vms that are not terminated to 'terminated'.
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('state !=', VMStateModel.STATE_TERMINATED)

            for e in entities:
                e.state = VMStateModel.STATE_TERMINATED
                e.put()
        except Exception as e:
            logging.error("Error in terminating all vms in db! {0}".format(e))


    @staticmethod
    def update_res_ids(params, ids, res_id):
        '''
        set the reservation id of the some entities given their ids.
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
            ids       a list of ids which are the primary keys of VMStateModel
            res_id    the reservation id that should be updated
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            entities = VMStateModel.get_by_id(ids)

            for e in entities:
                e.res_id = res_id
                e.put()
        except Exception as e:
            logging.error("Error in updating reservation ids in db! {0}".format(e))

    @staticmethod
    def update_ins_ids(params, ins_ids, res_id):
        '''
        set the instance ids within the certain reservation,
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
            ins_ids   a list of instance ids that are going to be set with
            res_id    the reservation id that is based on
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            entities = VMStateModel.all()
            entities.filter('infra =', infra).filter('access_key =', access_key).filter('secret_key =', secret_key)
            entities.filter('res_id =', res_id).filter('state =', VMStateModel.STATE_CREATING)

            for (ins_id, e) in zip(ins_ids, entities.run(limit=len(ins_ids))):
                e.ins_id = ins_id
                e.state = VMStateModel.STATE_PENDING
                e.put()
        except Exception as e:
            logging.error("Error in updating instance ids in db! {0}".format(e))

    @staticmethod
    def update_ips(params, ins_ids, pub_ips, pri_ips, ins_types, local_key):
        '''
        set the the public ips, the private ips, the instance types and the local key 
        to corresponding instance ids.
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
            ins_ids   a list of instance ids that are based on
            pub_ips   a list of public ips that are going to be set with
            pri_ips   a list of private ips that are going to be set with
            ins_type  a list of instance types that are going to be set with
            local_key the local key name that is corresponding to this set of instance ids
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            if local_key is None:
                raise Exception('Error: Cannot find local key!')
            for (ins_id, pub_ip, pri_ip, ins_type) in zip(ins_ids, pub_ips, pri_ips, ins_types):
                e = VMStateModel.all().filter('ins_id =', ins_id).filter('infra =', infra).filter('access_key =',
                                                                                                  access_key).filter(
                    'secret_key =', secret_key).get()
                e.pub_ip = pub_ip
                e.pri_ip = pri_ip
                e.ins_type = ins_type
                e.local_key = local_key
                e.put()
        except Exception as e:
            logging.error("Error in updating ips in db! {0}".format(e))

    @staticmethod
    def set_state(params, ins_ids, state, description=None):
        '''
        set the state of a list of instances and add some discriptions if needed.
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
            ins_ids   a list of instance ids that are going to be set state
            state     the state that is going to be set to the instances
            description    (optional) the description to the state     
        '''
        try:
            infra, access_key, secret_key = VMStateModel.validate_credentials(params)
            if infra is None:
                return

            for ins_id in ins_ids:
                e = VMStateModel.all().filter('ins_id =', ins_id).filter('infra =', infra).filter('access_key =',
                                                                                                  access_key).filter(
                    'secret_key =', secret_key).get()
                if e.state != VMStateModel.STATE_TERMINATED:
                    e.state = state
                if description is not None:
                    e.description = description
                e.put()
        except Exception as e:
            logging.error("Error in set state.")

    @staticmethod
    def validate_credentials(params):
        '''
        validate if the access key and secret key are available to be used
        Args
            params    a dictionary of parameters, containing at least 'agent' and 'credentials'.
        Return
            A tuple of the form (infrastructure, access key, secret key).
        '''
        infra = None
        access_key = None
        secret_key = None
        if 'infrastructure' in params:
            infra = params['infrastructure']
        else:
            raise Exception('VMStateModel ERROR: Infrastructure is not decided.')

        if 'credentials' in params:
            if 'EC2_ACCESS_KEY' in params['credentials'] and 'EC2_SECRET_KEY' in params['credentials']:
                access_key = params['credentials']['EC2_ACCESS_KEY']
                secret_key = params['credentials']['EC2_SECRET_KEY']
            else:
                raise Exception('VMStateModel ERROR: Cannot get access key or secret.')
        else:
            raise Exception('VMStateModel ERROR: No credentials are provided.')

        if infra is None or access_key is None or secret_key is None:
            raise Exception('VMStateModel ERROR: Either infrastracture or credetials is none.')

        return infra, access_key, secret_key

    @staticmethod
    def synchronize(agent, credentials):
        '''
        synchronization the db with the specific agent
        Args
            agent    the agent that is going to be synchronized with
            credentials    the dictionary containing access_key and secrest_key pair of the agent
        '''
        logging.info('Start Synchronizing DB...')
        instanceList = agent.describe_instances({'credentials': credentials})

        entities = VMStateModel.all()
        entities.filter('infra =', agent.AGENT_NAME).filter('access_key =', credentials['EC2_ACCESS_KEY']).filter(
            'secret_key =', credentials['EC2_SECRET_KEY'])
        entities.filter('state !=', VMStateModel.STATE_TERMINATED).filter('state !=', VMStateModel.STATE_CREATING)

        for e in entities:
            find = False
            for ins in instanceList:
                if e.ins_id == ins['id']:
                    find = True
                    if e.state == VMStateModel.STATE_PENDING and ins['state'] == VMStateModel.STATE_RUNNING:
                        break
                    else:
                        if ins['state'] == 'shutting-down':
                            ins['state'] = VMStateModel.STATE_TERMINATED
                        e.state = ins['state']
                        e.put()
                    break

            if not find:
                e.state = VMStateModel.STATE_TERMINATED
                e.decription = VMStateModel.DESCRI_NOT_FOUND
                e.put()
        logging.info('Finished synchronizing DB!')


class BackendWorker():
    '''
    BackendWorker does the job of spawning virtual machines on a specific agent and 
    configuring celery workers on those machines.
    Currently, it is implemented in a blocking mode.   
    '''

    KEYPREFIX = 'stochss'
    QUEUEHEAD_KEY_TAG = 'queuehead'


    def spawn_vms(self, infra, agent, parameters, reservation_id):
        """
        Public method for starting a set of VMs

        Args:
        infra           Infrastructure that is needed to be call
        agent           Infrastructure agent in charge of current operation
        parameters      A dictionary of parameters
        reservation_id  the reservation id for the instances that are going to be spawned
        """
        logging.info("AGENT_NAME = {0}".format(agent.AGENT_NAME))

        if not parameters["vms"] and 'head_node' not in parameters:
            logging.info("No vms are waiting for spawned.")
            return

        try:
            # ##################################################
            # step 1: run instance based on queue head or not #
            ###################################################

            num_vms = 0

            if not self.__is_queue_head_running(agent, parameters):
                # Queue head is not running, so create a queue head
                if 'head_node' not in parameters:
                    logging.info("Head node is needed.")
                    return

                utils.log('About to start a queue head.')

                parameters["queue_head"] = True
                requested_key_name = parameters["keyname"]

                # get the largest instance_type and let it to be queue head
                head_node = parameters['head_node']                
                parameters["instance_type"] = head_node["instance_type"]
                parameters["num_vms"] = 1
                num_vms = 1

                # Only want one queue head, and it must have its own key so
                # it can be differentiated if necessary
                parameters["keyname"] = requested_key_name + '-' + self.QUEUEHEAD_KEY_TAG

                security_configured = agent.configure_instance_security(parameters)
                try:
                    agent.run_instances(parameters)
                except Exception as e:
                    raise Exception('Errors in running instances in agent: ' + str(e))

                del parameters['head_node']
            # if the queue head is running
            else:
                if "queue_head" in parameters and parameters["queue_head"] == True:
                    parameters["keyname"] = parameters["keyname"].replace('-' + self.QUEUEHEAD_KEY_TAG, '')
                    logging.info('KEYNAME: {0}'.format(parameters["keyname"]))

                parameters["queue_head"] = False
                security_configured = agent.configure_instance_security(parameters)

                for vm in parameters["vms"]:
                    parameters["instance_type"] = vm["instance_type"]
                    parameters["num_vms"] = vm["num_vms"]
                    num_vms += vm["num_vms"]
                    try:
                        agent.run_instances(parameters)
                    except:
                        raise Exception('Errors in running instances in agent.')



            ########################################################################
            # step 2: poll the status of instances, if not running, terminate them #
            ########################################################################
            public_ips, private_ips, instance_ids = self.__poll_instances_status(infra, agent, num_vms, parameters,
                                                                                 reservation_id)
            if public_ips == None:
                if not self.__is_queue_head_running(agent, parameters):
                    # if last time of spawning queue head failed, spawn another queue head again
                    self.spawn_vms(infra, agent, parameters, reservation_id)
                else:
                    return

            ############################################################
            # step 3: set alarm for the nodes, if it is NOT queue head #
            ############################################################
            logging.info('Set shutdown alarm')

            try:
                if "queue_head" not in parameters or parameters["queue_head"] == False:
                    for ins_id in instance_ids:
                        agent.make_sleepy(parameters, ins_id)
                else:
                    agent.make_sleepy(parameters, instance_ids[0], '7200')

            except:
                raise Exception('Errors in set alarm for instances.')

            ########################################################
            # step 4: verify whether nodes are connectable via ssh #
            ########################################################
            connected_public_ips, connected_instance_ids = self.__verify_instances_via_ssh(agent, parameters,
                                                                                           public_ips, instance_ids)

            if len(connected_public_ips) == 0:
                if not self.__is_queue_head_running(agent, parameters):
                    # if last time of spawning queue head failed, spawn another queue head again
                    self.spawn_vms(infra, agent, parameters, reservation_id)
                else:
                    return


            #########################################
            # step 5: configure celery on each node #
            #########################################
            if "queue_head" in parameters and parameters["queue_head"] == True:
                queue_head_ip = connected_public_ips[0]
                utils.log('queue_head_ip: {0}'.format(queue_head_ip))
                # celery configuration needs to be updated with the queue head ip                 
                helper.update_celery_config_with_queue_head_ip(queue_head_ip=queue_head_ip,
                                                               agent_type=agent.AGENT_NAME)

            # copy celery configure to nodes.
            self.__configure_celery(agent, parameters, connected_public_ips, connected_instance_ids)


            #################################################################
            # step 6: if current node is queue head, may need to spwan the rest #
            #################################################################

            if "queue_head" in parameters and parameters["queue_head"] == True:
                self.spawn_vms(infra, agent, parameters, reservation_id)
            else:
                # else all vms requested are finished spawning. Done!
                return

        except Exception as e:
            logging.exception(e)


    def __poll_instances_status(self, infra, agent, num_vms, parameters, reservation_id):
        '''
        Private method that working on polling the state of instances that have already spawned 
        every some time and checking the ssh connectability if they are running.
        
        Args
            infra           Infrastructure that is needed to be call
            agent           Agent in charge of current operation
            num_vms         Number of virtual machines that are needed to be polling
            parameters      A dictionary of parameters
            reservation_id  the reservation id for the instances that are going to be spawned  
            
        Return
            A turple of (public ips, private ips, instance ids). Each of the three is a list
        '''
        utils.log('Start polling task.')

        ins_ids = agent.describe_instances_launched(parameters)
        # update db with new instance ids and 'pending'  
        VMStateModel.update_ins_ids(parameters, ins_ids, reservation_id)

        public_ips = None
        private_ips = None
        instance_ids = None

        POLL_COUNT = 10
        POLL_WAIT = 5
        for x in range(0, POLL_COUNT):
            # get the ips and ids of this keyname
            public_ips, private_ips, instance_ids, instance_types = agent.describe_instances_running(parameters)

            # if we get the requested number of vms (the requested number will be 1 if this is queue head),
            # update reservation information and send a message to the backend server
            if num_vms == len(public_ips):
                # update db with new public ips and private ips
                VMStateModel.update_ips(parameters, instance_ids, public_ips, private_ips, instance_types,
                                        parameters["keyname"])
                break

            else:
                if x < POLL_COUNT - 1:
                    time.sleep(POLL_WAIT)
                    utils.log('polling task: sleep 5 seconds...')
                else:
                    VMStateModel.update_ips(parameters, instance_ids, public_ips, private_ips, instance_types,
                                            parameters["keyname"])

                    logging.info('Polling timeout. About to terminate some instances:')
                    terminate_ins_ids = []
                    for ins_id in ins_ids:
                        if ins_id not in instance_ids:
                            logging.info('instance {0}'.format(ins_id))
                            terminate_ins_ids.append(ins_id)
                    # terminate timeout instances        
                    agent.terminate_some_instances(parameters, terminate_ins_ids)
                    # update db with failure information
                    VMStateModel.set_state(parameters, terminate_ins_ids, VMStateModel.STATE_FAILED,
                                           VMStateModel.DESCRI_FAIL_TO_RUN)

        return public_ips, private_ips, instance_ids


    def __verify_instances_via_ssh(self, agent, parameters, public_ips, instance_ids):
        '''
        Private method that is used to verify whether the virtual machines are connectable via ssh.
        
        Args
            agent           Agent in charge of current operation
            parameters      A dictionary of parameters
            public_ips      A list of public ips that are used for ssh connection
            instance_ids    A list of instance_ids that are used for terminating instances and update
                            database if fail on verification the instances by some reason
            
        Return
            A turple of (public ips, instance ids). Each of the two is a list of successfully verified
            instances
        '''
        utils.log('{0} nodes are running. Now trying to verify ssh connectable.'.format(len(public_ips)))

        # status_info = infra.reservations.get(reservation_id)

        keyfile = "{0}/../{1}.key".format(os.path.dirname(__file__), parameters['keyname'])
        if not os.path.exists(keyfile):
            raise Exception("ssh keyfile file not found: {0}".format(keyfile))

        connected_public_ips = []
        connected_instance_ids = []

        for (pub_ip, ins_id) in zip(public_ips, instance_ids):

            logging.info('connecting to {0}...'.format(pub_ip))
            success = helper.wait_for_ssh_connection(key_file=keyfile, ip=pub_ip)

            if success == True:
                logging.info('{0} is successfully added'.format(pub_ip))
                connected_public_ips.append(pub_ip)
                connected_instance_ids.append(ins_id)

        # if there are some vms not able to be connected via ssh, 
        # just shut them down explicitly            
        if len(public_ips) != len(connected_public_ips):
            logging.info('Time out on ssh to {0} instances. They will be terminated.'.format(
                len(public_ips) - len(connected_public_ips)))

            try:
                terminate_ins_ids = []
                for ins_id in instance_ids:
                    if ins_id not in connected_instance_ids:
                        terminate_ins_ids.append(ins_id)
                agent.terminate_some_instances(parameters, terminate_ins_ids)
                # update db with failed vms 
                VMStateModel.set_state(parameters, terminate_ins_ids, VMStateModel.STATE_FAILED,
                                       VMStateModel.DESCRI_TIMEOUT_ON_SSH)
            except:
                raise Exception("Errors in terminating instances that cannot be connected via ssh.")

        public_ips = None
        instance_ids = None

        return connected_public_ips, connected_instance_ids


    def __configure_celery(self, agent, params, public_ips, instance_ids):
        '''
        Private method used for uploading the current celery configuration to each instance 
        that is running and ssh connectable.
        
        Args
            agent           Agent in charge of current operation
            parameters      A dictionary of parameters
            public_ips      A list of public ips that are going to be configed
            instance_ids    A list of instance_ids that are used for terminating instances and update
                            database if fail on configuration by some reason  
        '''
        # Update celery config file...it should have the correct IP
        # of the Queue head node, which should already be running.
        # Pass it line by line so theres no weird formatting errors from
        # trying to echo a multi-line file directly on the command line


        key_file = os.path.join(os.path.dirname(__file__), '..', '{0}.key'.format(params['keyname']))
        logging.debug("key_file = {0}".format(key_file))

        if not os.path.exists(key_file):
            raise Exception("ssh key_file file not found: {0}".format(key_file))

        credentials = params['credentials']

        commands = []
        commands.append('source /home/ubuntu/.bashrc')
        commands.append('export AWS_ACCESS_KEY_ID={0}'.format(str(credentials['EC2_ACCESS_KEY'])))
        commands.append('export AWS_SECRET_ACCESS_KEY={0}'.format(str(credentials['EC2_SECRET_KEY'])))

        for ip, ins_id in zip(public_ips, instance_ids):
            # helper.wait_for_ssh_connection(key_file, ip)
            ins_type = VMStateModel.get_instance_type(params, ins_id)
            success = helper.start_celery_on_vm(instance_type=ins_type, ip=ip, key_file=key_file,
                                                agent_type=AgentTypes.EC2,
                                                prepend_commands=commands)
            if success == 0:
                # update db with successful running vms    
                VMStateModel.set_state(params, [ins_id], VMStateModel.STATE_RUNNING, VMStateModel.DESCRI_SUCCESS)
                logging.info("celery started! ")
                logging.info("host ip: {0}".format(ip))

            else:
                agent.terminate_some_instances(params, [ins_id])
                VMStateModel.set_state(params, [ins_id], VMStateModel.STATE_FAILED,
                                       VMStateModel.DESCRI_FAIL_TO_COFIGURE_CELERY)
                raise Exception("Failure to start celery on {0}".format(ip))

        # get all intstance types and configure the celeryconfig.py locally
        instance_types = VMStateModel.get_running_instance_type(params)
        helper.config_celery_queues(agent_type=AgentTypes.EC2, instance_types=instance_types)


    def __is_queue_head_running(self, agent, params):
        '''
        Private method that is used for checking whether the queue head is running. Queue head has
        a different configuration of machine type and should be used for celery configuration.
        
        Args
            agent    Agent in charge of current operation
            params   A dictionary of parameters
            
        Return
            A boolean value of wether the queue head is running or not
        '''

        params['key_prefix'] = self.KEYPREFIX
        try:
            all_vms = agent.describe_instances(params, params['key_prefix'])
            if all_vms == None:
                logging.info('No vms are found.')
                return False
            # Just need one running vm with the QUEUEHEAD_KEY_TAG in the name of the keypair
            for vm in all_vms:
                if vm != None and vm['state'] == 'running' and vm['key_name'].find(self.QUEUEHEAD_KEY_TAG) != -1:
                    return True
            return False
        except Exception as e:
            logging.error('Error in testing queuehead running. {0}'.format(e))
            return False


class SynchronizeDB(webapp2.RequestHandler):
    '''
    SynchronizeDB's main job is to start a background_thread to synchronize the VMStateModel
    with the agent every some time
    '''
    PAUSE = 180

    def post(self):
        req_agent = self.request.get('agent')
        self.agent = pickle.loads(str(req_agent))
        req_parameters = self.request.get('parameters')
        self.parameters = pickle.loads(str(req_parameters))

        self.is_start = False

        id = background_thread.start_new_background_thread(self.begin, [])
        logging.info('Started a background thread to synchronize db. id: {0}'.format(id))
        return

    def begin(self):
        try:
            self.credentials = self.parameters['credentials']
        except:
            raise Exception('Error: credentials not set properly!')

        if not self.is_start:
            self._run()

    def _start(self):
        if not self.is_start:
            self.timer = Timer(SynchronizeDB.PAUSE, self._run).start()
            self.is_start = True

    def _run(self):
        self.is_start = False
        VMStateModel.synchronize(self.agent, self.credentials)
        self._write_time()
        self._start()

    def _write_time(self):
        try:
            now = datetime.datetime.now()
            logging.info('Write datetime of synchronization to db_syn: {0}'.format(now))
            syn = VMStateSyn.all()
            if syn.count() == 0:
                e = VMStateSyn(last_syn=now)
                e.put()
            else:
                e = db.GqlQuery("SELECT * FROM VMStateSyn").get()
                e.last_syn = now
                e.put()

        except Exception as e:
            logging.error('Error: have errors in write date time to db_syn. {0}'.format(e))


# @staticmethod
#     def stop():
#         SynchronizeDB.TIMER.cancel()
#         SynchronizeDB.SYNCHRONIZE_DB = False

class BackendQueue(webapp2.RequestHandler):
    '''
    BackendQueue is a task queue that runs in the background, in parallel with the front end.
    Currently, there are 2 types of tasks that BackendQueue is going to deal with:
        1. start vms that front end requests (operation code: start_vms)
        2. trigger the database auto synchronization (operation code: start_db_syn). 
        Since currently, we could not solve the background http 500 error interruption from somewhere
        using the background_thread, we have to check the db synchronization log every sometime
        and start another background_thread if the previous one stop working by background http 500.
    The tasks in the task queue are executed serially in block mode.   
    '''

    def get(self):
        op = self.request.get('op')
        if op == 'start_vms':
            utils.log('Backend queue got the request to start vms.')
            # pickle all loading objects
            req_infra = self.request.get('infra')
            infra = pickle.loads(str(req_infra))
            req_agent = self.request.get('agent')
            agent = pickle.loads(str(req_agent))
            req_parameters = self.request.get('parameters')
            parameters = pickle.loads(str(req_parameters))
            req_reservation_id = self.request.get('reservation_id')
            reservation_id = pickle.loads(str(req_reservation_id))

            # execute spawning vms
            worker = BackendWorker()
            worker.spawn_vms(infra, agent, parameters, reservation_id)
            utils.log('Backend queue finished starting vms.')

        elif op == 'start_db_syn':
            utils.log('Backend queue got the request to start syn db.')

            # trigger db auto synchronization 
            urlfetch.fetch(url=BACKEND_SYN_R_URL,
                           method=urlfetch.POST,
                           payload=urllib.urlencode(self.request.GET))


app = webapp2.WSGIApplication([('/backend/synchronizedb', SynchronizeDB),
                               ('/backend/queue', BackendQueue)],
                              debug=True)