import sys,os,logging
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/celery'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/kombu'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/amqp'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/billiard'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/anyjson'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../lib/pytz'))
print str(sys.path)
from celery import Celery
import os
import uuid,traceback
THOME = '/home/ubuntu'
STOCHKIT_DIR = '/home/ubuntu/StochKit'
ODE_DIR = '/home/ubuntu/ode'

celery = Celery('tasks')
celery.config_from_object('celeryconfig')
import logging, subprocess
import boto.dynamodb
from datetime import datetime

@celery.task(name='stochss')
def task(taskid,params):
  ''' This is the actual work done by a task worker '''
  try:
        
      global THOME
      global STOCHKIT_DIR
      global ODE_DIR

      print 'task to be executed at remote location'
      print 'inside celery task method'
      data = {'status':'active','message':'Task Executing in cloud'}
      updateEntry(taskid, data, "stochss")
      res = {}
      paramstr =  params['paramstring']
      uuidstr = taskid
      res['uuid'] = uuidstr
      create_dir_str = "mkdir -p output/%s/result " % uuidstr
      os.system(create_dir_str)
      filename = "output/{0}/{0}.xml".format(uuidstr)
      f = open(filename,'w')
      f.write(params['document'])
      f.close()
      xmlfilepath = filename
      stdout = "output/%s/stdout.log" % uuidstr
      stderr = "output/%s/stderr.log" % uuidstr

      job_type = params['job_type']
      exec_str = ''
      if job_type == 'stochkit':
          # The following executiong string is of the form : stochkit_exec_str = "~/StochKit/ssa -m ~/output/%s/dimer_decay.xml -t 20 -i 10 -r 1000" % (uuidstr)
          exec_str = "{0}/{1} -m {2} --force --out-dir output/{3}/result 2>{4} > {5}".format(STOCHKIT_DIR, paramstr, xmlfilepath, uuidstr, stderr, stdout)
      elif job_type == 'stochkit_ode':
          exec_str = "{0}/{1} -m {2} --force --out-dir output/{3}/result 2>{4} > {5}".format(ODE_DIR, paramstr, xmlfilepath, uuidstr, stderr, stdout)
      
      print "======================="
      print " Command to be executed : "
      print exec_str
      print "======================="
      print "To test if the command string was correct. Copy the above line and execute in terminal."
      timestarted = datetime.now()
      os.system(exec_str)
      timeended = datetime.now()
      
      results = os.listdir("output/{0}/result".format(uuidstr))
      if 'stats' in results and os.listdir("output/{0}/result/stats".format(uuidstr)) == ['.parallel']:
          raise Exception("The compute node can not handle a job of this size.")
      
      res['pid'] = taskid
      filepath = "output/%s//" % (uuidstr)
      absolute_file_path = os.path.abspath(filepath)
      print 'generating tar file'
      create_tar_output_str = "tar -zcvf output/{0}.tar output/{0}".format(uuidstr)
      print create_tar_output_str
      logging.debug("followig cmd to be executed %s" % (create_tar_output_str))
      bucketname = params['bucketname']
      copy_to_s3_str = "python {2}/sccpy.py output/{0}.tar {1}".format(uuidstr,bucketname,THOME)
      data = {'status':'active','message':'Task finished. Generating output.'}
      updateEntry(taskid, data, "stochss")
      os.system(create_tar_output_str)
      print 'copying file to s3 : {0}'.format(copy_to_s3_str)
      os.system(copy_to_s3_str)
      print 'removing xml file'
      removefilestr = "rm {0}".format(xmlfilepath)
      os.system(removefilestr)
      removetarstr = "rm output/{0}.tar".format(uuidstr)
      os.system(removetarstr)
      removeoutputdirstr = "rm -r output/{0}".format(uuidstr)
      os.system(removeoutputdirstr)
      res['output'] = "https://s3.amazonaws.com/{1}/output/{0}.tar".format(taskid,bucketname)
      res['status'] = "finished"
      diff = timeended - timestarted
      res['time_taken'] = "{0} seconds and {1} microseconds ".format(diff.seconds, diff.microseconds)
      updateEntry(taskid, res, "stochss")
  except Exception,e:
      expected_output_dir = "output/%s" % uuidstr
      # First check for existence of output directory
      if os.path.isdir(expected_output_dir):
          # Then we should store this in S3 for debugging purposes
          create_tar_output_str = "tar -zcvf {0}.tar {0}".format(expected_output_dir)
          os.system(create_tar_output_str)
          bucketname = params['bucketname']
          copy_to_s3_str = "python {0}/sccpy.py {1}.tar {2}".format(THOME, expected_output_dir, bucketname)
          os.system(copy_to_s3_str)
          # Now clean up
          remove_output_str = "rm {0}.tar {0}".format(expected_output_dir)
          os.system(remove_output_str)
          # Update the DB entry
          res['output'] = "https://s3.amazonaws.com/{0}/{1}.tar".format(bucketname, expected_output_dir)
          res['status'] = 'failed'
          res['message'] = str(e)
          updateEntry(taskid, res, "stochss")
      else:
          # Nothing to do here besides send the exception
          data = {'status':'failed', 'message':str(e)}
          updateEntry(taskid, data, "stochss")
      raise e
  return res


def checkStatus(task_id):
    '''
    Method takes task_id as input and returns the result of the celery task
    '''
    logging.info("checkStatus inside method with params %s", str(task_id))
    result = {}
    try:
        from celery.result import AsyncResult
        res = AsyncResult(task_id)
        logging.debug("checkStatus: result returned for the taskid = {0} is {1}".format(task_id, str(res)))
        result = res.result
        result['state'] = res.status
        if res.status == "PROGRESS":
            print 'Task in progress'
            print 'Current %d' % result['current']
            print 'Total %d' % result['total']
            result['result'] = None
        elif res.status == "SUCCESS":
            result['result'] = res.result
        elif res.status == "FAILURE":
            result['result'] = res.result 
        
    except Exception, e:
        logging.debug("checkStatus error : %s", str(e))
        result['state'] = "FAILURE"
        result['result'] = str(e)
    logging.debug("checkStatus : Exiting with result %s", str(res))
    return result


def removeTask(task_id):
    '''
    this method revokes scheduled tasks as well as the tasks in progress
    '''
    try:
        print "removeTask: with task_id: {0}".format(task_id)
        from celery.task.control import revoke
        # Celery can't use remote control (which includes revoking tasks) with SQS
        # http://docs.celeryproject.org/en/latest/getting-started/brokers/sqs.html
        revoke(task_id)#, terminate=True, signal="SIGTERM")
    except Exception,e:
        print "task {0} cannot be removed/deleted. Error : {1}".format(task_id, str(e))
        
#def describeTask():
#    i = celery.control.inspect()
#    print type(i)
#    print dir(i)
#    print str(i.active_queues())
#    print str(i.registered_tasks())
#    print str(i.stats())
#    #print str(i.registered())
#    #print str(i.active())
#    #print str(i.scheduled())

"""
All DynamoBD related methods follow next. TODO: move it to a different file
"""

def describetask(taskids,tablename):
    res = {}
    try:
        print 'inside describetask method with taskids = {0} and tablename {1}'.format(str(taskids), tablename)
        dynamo=boto.connect_dynamodb()
        if not tableexists(dynamo, tablename): return res
        table = dynamo.get_table(tablename)
        for taskid in taskids:
            try:
                item = table.get_item(hash_key=taskid)
                res[taskid] = item
            except Exception,e:
                res[taskid] = None
        return res
    except Exception,e:
        print "exiting describetask  with error : {0}".format(str(e))
        print str(e)
        return res

def removetask(tablename,taskid):
    print 'inside removetask method with tablename = {0} and taskid = {1}'.format(tablename, taskid)
    try:
        dynamo=boto.connect_dynamodb()
        if tableexists(dynamo, tablename):
            table = dynamo.get_table(tablename)
            item = table.get_item(hash_key=taskid)
            item.delete()
            return True
        else:
            print 'exiting removetask with error : table doesn\'t exists'
            return False
        
    except Exception,e:
        print 'exiting removetask with error {0}'.format(str(e))
        return False
    
def createtable(tablename=str()):
    print 'inside create table method with tablename :: {0}'.format(tablename)
    if tablename == None:
        tablename = "stochss"
        print 'default table name picked as stochss'
    try:
        print 'connecting to dynamodb'
        dynamo=boto.connect_dynamodb()
        #check if table already exisits
        print 'checking if table {0} exists'.format(tablename)
        if not tableexists(dynamo,tablename):
            print 'creating table schema'
            myschema=dynamo.create_schema(hash_key_name='taskid',hash_key_proto_value=str)
            table=dynamo.create_table(name=tablename, schema=myschema, read_units=6, write_units=4)
        else:
            print "table already exists"
        return True  
    except Exception,e:
        print str(e)
        return False

def tableexists(dynamo, tablename):
    try:
        table = dynamo.get_table(tablename)
        if table == None:
            print "table doesn't exist"
            return False
        else:
            return True
    except Exception,e:
        print str(e)
        return False

def updateEntry(taskid=str(), data=dict(), tablename=str()):
    '''
     check if entry exists
     create a entry if not or
     update the status
    '''
    try:
        print 'inside update entry method with taskid = {0} and data = {1}'.format(taskid, str(data))
        dynamo=boto.connect_dynamodb()
        if not tableexists(dynamo, tablename):
            print "invalid table name specified"
            return False
        table = dynamo.get_table(tablename)
        item = table.new_item(hash_key=str(taskid),attrs=data)
        item.put()
        return True
    except Exception,e:
        print 'exiting updatedata with error : {0}'.format(str(e))
        return False
    
if __name__ == "__main__":

    '''
    NOTE: these must be set in your environment:
    export AWS_SECRET_ACCESS_KEY=XXX
    export AWS_ACCESS_KEY_ID=YYY
    '''
    global THOME
    global STOCHKIT_DIR
    os.environ["AWS_ACCESS_KEY_ID"] = os.environ['EC2_ACCESS_KEY']
    os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ['EC2_SECRET_KEY']

    print createtable('stochss')
    val = {'status':"running", 'message':'done'}
    updateEntry('1234', val, 'stochss')
    print describetask(['1234', '1234'], 'stochss')
    
    #this executes a task locally
    #NOTE: dimer_decay.xml must be in this local dir
    xmlfile = open('dimer_decay.xml','r')
    doc = xmlfile.read()
    xmlfile.close()
    taskargs = {}
    taskargs['paramstring'] = 'ssa -t 100 -i 1000 -r 100 --keep-trajectories --seed 706370 --label'
    taskargs['document'] = doc
    taskargs['bucketname'] = 'cjk1234'

    THOME=os.getcwd()
    STOCHKIT_DIR='{0}/../../StochKit'.format(THOME)
    task('1234',taskargs)
    print describetask(['1234', '1234'], 'stochss')
 
    print 'BE SURE TO GO TO YOUR AWS ADMIN CONSOLE AND DELETE DYNAMODB TABLES AND S3 BUCKETS'
    

