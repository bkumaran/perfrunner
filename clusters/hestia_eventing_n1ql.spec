[clusters]
hestia =
    172.23.99.203:kv,n1ql
    172.23.99.204:kv,n1ql
    172.23.99.205:kv,n1ql
    172.23.99.206:kv,n1ql
    172.23.97.177:eventing

[clients]
hosts =
    172.23.99.200
credentials = root:couchbase

[storage]
data = /data
index = /data

[credentials]
rest = Administrator:password
ssh = root:couchbase

[parameters]
OS = CentOS 7
CPU = E5-2630 (24 vCPU)
Memory = 64GB
Disk = Samsung Pro 850
