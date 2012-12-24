from ConfigParser import SafeConfigParser
from boto.s3.connection import Location
from collections import namedtuple
import os, boto, boto.ec2

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), 'venv', 'aws.cfg')
BASTION_KEY_FILE = os.path.join(os.path.dirname(__file__), 'venv', 'bastion.pem')

ANYWHERE = '0.0.0.0/0'

Node = namedtuple('Node', ['name', 'public_ip', 'user', 'keyfile'])

def make_vpc(vpc_config_name):
    vpc_config = read_vpc_config(vpc_config_name)

    vpc_name = vpc_config.get('vpc', 'name')
    vpc_region = vpc_config.get('vpc', 'region')

    access_key_id, secret_access_key = read_credentials()
    vpc_conn = boto.connect_vpc(access_key_id, secret_access_key, region=vpc_region)
    ec2_conn = boto.ec2.connect_to_region(vpc_region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)
    s3_conn = boto.connect_s3(access_key_id, secret_access_key)

    vpc = get_or_create_vpc(vpc_conn, vpc_name, vpc_config.get('vpc', 'cidr_block'))
    internet_gateway = get_or_create_internet_gateway(vpc_conn, vpc, vpc_name)
    route_table = get_or_create_route_table(vpc_conn, vpc, 'public', internet_gateway)

    bastion_hosts = []
    for subnet_name in vpc_config.sections():
        if subnet_name != 'vpc':
            cidr_block = vpc_config.get(subnet_name, 'cidr_block')
            bastion_name = vpc_config.get(subnet_name, 'bastion_host')
            availability_zone = vpc_config.get(subnet_name, 'availability_zone')
            subnet = get_or_create_subnet(vpc_conn, vpc, route_table, subnet_name, cidr_block, availability_zone)
            bastion = get_or_create_bastion_host(ec2_conn, s3_conn, vpc_config, bastion_name, vpc.id, subnet)
            bastion_hosts.append(bastion)

    return bastion_hosts

def read_vpc_config(vpc_config_name):
    vpc_config_file = os.path.join(os.path.dirname(__file__), 'config', 'vpc', vpc_config_name + '.cfg')
    return read_config_file(vpc_config_file)

def get_or_create_vpc(vpc_conn, vpc_name, cidr_block):
    for vpc in filter_by_name(vpc_conn.get_all_vpcs, vpc_name):
        return vpc
    print 'Creating VPC with name:', vpc_name
    vpc = vpc_conn.create_vpc(cidr_block)
    tag_with_name(vpc, vpc_name)
    return vpc

def get_or_create_internet_gateway(vpc_conn, vpc, vpc_name):
    for internet_gateway in filter_by_name(vpc_conn.get_all_internet_gateways, vpc_name):
        return internet_gateway
    print 'Creating Internet Gateway with name:', vpc_name
    internet_gateway = vpc_conn.create_internet_gateway()
    tag_with_name(internet_gateway, vpc_name)
    vpc_conn.attach_internet_gateway(internet_gateway.id, vpc.id)
    return internet_gateway

def get_or_create_route_table(vpc_conn, vpc, route_name, internet_gateway):
    for route_table in filter_by_name_and_vpc(vpc_conn.get_all_route_tables, route_name, vpc.id):
        return route_table
    print 'Creating Route Table with name:', route_name
    route_table = vpc_conn.create_route_table(vpc.id)
    tag_with_name(route_table, route_name)
    vpc_conn.create_route(route_table.id, ANYWHERE, internet_gateway)
    return route_table

def get_or_create_subnet(vpc_conn, vpc, route_table, subnet_name, cidr_block, availability_zone):
    for subnet in filter_by_name_and_vpc(vpc_conn.get_all_subnets, subnet_name, vpc.id):
        return subnet
    print 'Creating Subnet with name:', subnet_name, 'in:', availability_zone
    subnet = vpc_conn.create_subnet(vpc.id, cidr_block, availability_zone)
    tag_with_name(subnet, subnet_name)
    vpc_conn.associate_route_table(route_table.id, subnet.id)
    return subnet

def get_or_create_bastion_host(ec2_conn, s3_conn, vpc_config, bastion_host_name, vpc_id, subnet):
    image_id = vpc_config.get('vpc', 'default_image_id')
    instance_type = vpc_config.get('vpc', 'default_instance_type')
    image_login_user = vpc_config.get('vpc', 'default_image_login_user')
    key_pair = get_bastion_host_key(ec2_conn, s3_conn, vpc_config)
    security_group = get_or_create_vpc_security_group(ec2_conn, vpc_id)

    for reservation in fetch_running_reservations(ec2_conn, bastion_host_name):
        for instance in reservation.instances:
            return Node(bastion_host_name, instance.ip_address, image_login_user, BASTION_KEY_FILE)

    print 'Creating Instance with name:', bastion_host_name
    reservation = ec2_conn.run_instances(image_id, instance_type=instance_type,
                        key_name=key_pair.name, security_group_ids=[security_group.id],
                        subnet_id=subnet.id)

    for instance in reservation.instances:
        print 'Waiting for', bastion_host_name, instance.id, 'to start ...'
        wait_until(instance, 'running')
        tag_with_name(instance, bastion_host_name)
        elastic_ip = get_or_create_elastic_ip(ec2_conn)
        ec2_conn.associate_address(instance.id, elastic_ip)
        print bastion_host_name, 'is associated with Elastic IP', elastic_ip
        return Node(bastion_host_name, elastic_ip, image_login_user, BASTION_KEY_FILE)

def get_or_create_elastic_ip(ec2_conn):
    filters = {'domain': 'vpc'}
    for address in ec2_conn.get_all_addresses(filters=filters):
        if not address.instance_id:
            return address.public_ip
    print 'Creating a new Elastic IP'
    address = ec2_conn.allocate_address('vpc')
    return address.public_ip

def get_bastion_host_key(ec2_conn, s3_conn, vpc_config):
    vpc_name = vpc_config.get('vpc', 'name')
    key_name = vpc_name + '-bastion'
    key_object_name = key_name + '.pem'
    key_pair = get_or_create_bastion_key_pair(ec2_conn, s3_conn, key_name, key_object_name, vpc_config)
    ensure_bastion_host_keyfile_exists(s3_conn, vpc_config, key_object_name)
    return key_pair

def get_or_create_bastion_key_pair(ec2_conn, s3_conn, key_name, key_object_name, vpc_config):
    key_pair = ec2_conn.get_key_pair(key_name)
    if not key_pair:
        print 'Creating KeyPair with name:', key_name
        key_pair = ec2_conn.create_key_pair(key_name)
        write_bastion_key_file(key_pair.material)
        upload_bastion_key(s3_conn, vpc_config, key_object_name)
    return key_pair

def write_bastion_key_file(contents):
    with open(BASTION_KEY_FILE, 'wb') as fp:
        fp.write(contents)
    os.chmod(BASTION_KEY_FILE, 0600)

def upload_bastion_key(s3_conn, vpc_config, key_object_name):
    print 'Uploading bastion host key to S3 bucket'
    bucket = get_key_bucket(s3_conn, vpc_config)
    key = bucket.new_key(key_object_name)
    key.set_contents_from_filename(BASTION_KEY_FILE, encrypt_key=True)

def ensure_bastion_host_keyfile_exists(s3_conn, vpc_config, key_object_name):
    if not os.path.isfile(BASTION_KEY_FILE):
        print 'Downloading bastion host key from S3 bucket'
        bucket = get_key_bucket(s3_conn, vpc_config)
        key = bucket.get_key(key_object_name)
        key.get_contents_to_filename(BASTION_KEY_FILE)
        os.chmod(BASTION_KEY_FILE, 0600)

def get_key_bucket(s3_conn, vpc_config):
    key_bucket_region = get_or_default(vpc_config, 'vpc', 'key_bucket_region', Location.DEFAULT)
    key_bucket_prefix = vpc_config.get('vpc', 'key_bucket_prefix')
    bucket_name = (key_bucket_prefix + s3_conn.aws_access_key_id).lower()
    print 'Using', bucket_name, 'to hold EC2 keys'
    return s3_conn.create_bucket(bucket_name, location=key_bucket_region)

def get_or_default(config, section, option, default_value=None):
    return config.get(section, option) if config.has_option(section, option) else default_value

def get_or_create_vpc_security_group(ec2_conn, vpc_config, vpc_id):
    vpc_name = vpc_config.get('vpc', 'name')
    security_group_name = vpc_name + '-bastion'

    for security_group in ec2_conn.get_all_security_groups([security_group_name]):
        return security_group

    print 'Creating Security Group with name:', security_group_name
    security_group ec2_conn.create_security_group(security_group_name, security_group_name, vpc_id)
    clear_all_permissions(ec2_conn, security_group) # start with a clean slate
    allow_https_egress(ec2_conn, security_group.id, ANYWHERE)
    allow_http_egress(ec2_conn, security_group.id, ANYWHERE)
    allow_ssh_ingress(ec2_conn, security_group.id, ANYWHERE)
    return security_group

def allow_ssh_ingress(ec2_conn, security_group, source):
    ec2_conn.authorize_security_group(group_id=security_group, ip_protocol='tcp', from_port=22, to_port=22, cidr_ip=source)

def allow_http_egress(ec2_conn, security_group, destination):
    ec2_conn.authorize_security_group_egress(security_group, 'tcp', 80, 80, None, destination)

def allow_https_egress(security_group, destination):
    ec2_conn.authorize_security_group_egress(security_group, 'tcp', 443, 443, None, destination)

def fetch_running_reservations(ec2_conn, name):
    filters = {'tag:Name': name, 'instance-state-name': 'running'}
    return ec2_conn.get_all_instances(filters=filters)

def filter_by_name(function, name):
    filters = {'tag:Name': name}
    return function(filters=filters)

def filter_by_name_and_vpc(function, name, vpc_id):
    filters = {'tag:Name': name, 'vpc-id': vpc_id}
    return function(filters=filters)

def tag_with_name(item, name):
    item.add_tag('Name', name)

def wait_until(instance, status):
    instance.update()
    while instance.state != status:
        time.sleep(5)
        instance.update()

def has_credentials():
    return os.path.isfile(CREDENTIALS_FILE)

def save_credentials(access_key_id, secret_access_key):
    config = SafeConfigParser()
    config.add_section('aws')
    config.set('aws', 'access_key_id', access_key_id)
    config.set('aws', 'secret_access_key', secret_access_key)
    with open(CREDENTIALS_FILE, 'w') as fp:
        config.write(fp)
    os.chmod(CREDENTIALS_FILE, 0600)

def read_credentials():
    config = read_config_file(CREDENTIALS_FILE)
    access_key_id = config.get('aws', 'access_key_id')
    secret_access_key = config.get('aws', 'secret_access_key')
    return (access_key_id, secret_access_key)

def read_config_file(config_file_path):
    config = SafeConfigParser()
    with open(vpc_config_file) as fp:
        config.readfp(fp)
    return config

def delete_vpc(vpc_config_name):
    vpc_config = read_vpc_config(vpc_config_name)

    vpc_name = vpc_config.get('vpc', 'name')
    vpc_region = vpc_config.get('vpc', 'region')

    access_key_id, secret_access_key = read_credentials()
    vpc_conn = boto.connect_vpc(access_key_id, secret_access_key, region=vpc_region)
    ec2_conn = boto.ec2.connect_to_region(vpc_region, aws_access_key_id=access_key, aws_secret_access_key=secret_access_key)

    for vpc in filter_by_name(vpc_conn.get_all_vpcs, vpc_name):
        filters = {'vpc-id': vpc.id}
        instances = [instance for instance in ec2_conn.get_all_instances(filters=filters)]

        filters = {'instance-id': [instance.id for instance in instances]}
        addresses = [address for address in ec2_conn.get_all_addresses(filters=filters)]

        for address in addresses:
            print 'Disassociating elastic ip', address.public_ip
            address.disassociate()

        for address in addresses:
            print 'Releasing elastic ip', address.public_ip
            address.release()

        for instance in instances:
            print 'Terminating instance', instance.id
            instance.terminate()

        for instance in instances:
            print 'Waiting for instance', instance.id, 'to terminate'
            wait_until(instance, 'terminated')

        # there is no filter to get security groups for a vpc!!
        security_groups = [group for group in ec2_conn.get_all_security_groups() if group.vpc_id == vpc.id]

        for security_group in security_groups:
            clear_all_permissions(vpc_conn, security_group)

        for security_group in security_groups:
            print 'Deleting security group', security_group.name
            security_group.delete()

        filters = {'vpc-id': vpc.id}

        for subnet in vpc_conn.get_all_subnets(filters=filters):
            print 'Deleting subnet', subnet.id
            vpc_conn.delete_subnet(subnet.id)

        for route_table in vpc.get_all_route_tables(filters=filters):
            print 'Deleting route table', route_table.id
            vpc_conn.delete_route_table(route_table.id)

        for internet_gateway in filter_by_name(vpc_conn.get_all_internet_gateways, vpc_name):
            print 'Deleting internet gateway', internet_gateway.id
            vpc_conn.delete_internet_gateway(internet_gateway.id)

        print 'Deleting vpc', vpc_name, vpc.id
        vpc_conn.delete_vpc(vpc.id)

def clear_all_permissions(ec2_conn, security_group):
    revoke_ingress_permissions(vpc_conn, security_group)
    revoke_egress_permissions(vpc_conn, security_group)

def revoke_ingress_permissions(ec2_conn, security_group):
    for rule in security_group.rules:
        for grant in rule.grants:
            ec2_conn.revoke_security_group(group_id=security_group.id, ip_protocol=rule.ip_protocol,
                from_port=rule.from_port, to_port=rule.to_port, cidr_ip=grant.cidr_ip,
                src_security_group_group_id=grant.group_id)

def revoke_egress_permissions(ec2_conn, security_group):
    for rule in security_group.rules_egress:
        for grant in rule.grants:
            ec2_conn.revoke_security_group_egress(security_group.id, rule.ip_protocol,
                rule.from_port, rule.to_port, grant.group_id, grant.cidr_ip)