package main

import (
	"fmt"
	c "github.com/couchbase/indexing/secondary/common"
	qclient "github.com/couchbase/indexing/secondary/queryport/client"
	"io"
	"math"
	"math/rand"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

var (
	defaultLatencyBuckets = []int64{
		500, 1000, 10000, 100000, 500000, 1000000, 5000000, 10000000, 50000000,
		100000000, 300000000, 500000000, 1000000000, 2000000000, 3000000000,
		5000000000, 10000000000,
	}

	clientBootTime = 5 // Seconds

	requestCounter = uint64(0)
)

type Job struct {
	spec   *ScanConfig
	result *ScanResult
	sw     io.Writer
}

type JobResult struct {
	job  *Job
	rows int64
	dur  int64
}

func Init() {
	rand.Seed(time.Now().UnixNano())
}

func RandString(n uint32, low string, high string) []interface{} {
	if lowInt, err := strconv.ParseInt(low, 16, 64); err == nil {
		if highInt, err := strconv.ParseInt(high, 16, 64); err == nil {
			randomInt := rand.Intn(int(highInt - lowInt - 2))
			lowInt2 := int(lowInt) + randomInt
			format := fmt.Sprintf("%%%03dx", n)
			low = fmt.Sprintf(format, lowInt2)
		}
	}
	return []interface{}{low}
}

func RunJob(client *qclient.GsiClient, job *Job, aggrQ chan *JobResult) {
	var err error
	var rows int64

	spec := job.spec
	result := job.result
	if result != nil {
		result.Id = spec.Id
	}

	errFn := func(e string) {
		fmt.Printf("REQ:%d scan error occurred: %s\n", spec.Id, e)
		if result != nil {
			atomic.AddUint64(&result.ErrorCount, 1)
		}
	}

	callb := func(res qclient.ResponseReader) bool {
		if res.Error() != nil {
			errFn(res.Error().Error())
			return false
		} else {
			_, pkeys, err := res.GetEntries()
			if err != nil {
				errFn(err.Error())
				return false
			}

			rows += int64(len(pkeys))
		}

		return true
	}

	var cons c.Consistency
	if spec.Consistency {
		cons = c.SessionConsistency
	} else {
		cons = c.AnyConsistency
	}

	startTime := time.Now()
	uuid := fmt.Sprintf("%d", atomic.AddUint64(&requestCounter, 1))
	switch spec.Type {
	case "All":
		requestID := os.Args[0] + uuid
		err = client.ScanAll(spec.DefnId, requestID, spec.Limit, cons, nil, callb)
	case "Range":
		requestID := os.Args[0] + uuid
		low := spec.Low
		if spec.RandomLen > 0 {
			low = RandString(spec.RandomLen, spec.Low[0].(string), spec.High[0].(string))
		}
		err = client.Range(spec.DefnId, requestID, low, spec.High,
			qclient.Inclusion(spec.Inclusion), false, spec.Limit, cons, nil, callb)
	case "Lookup":
		requestID := os.Args[0] + uuid
		err = client.Lookup(spec.DefnId, requestID, spec.Lookups, false,
			spec.Limit, cons, nil, callb)
	case "MultiScan":
		requestID := os.Args[0] + uuid
		err = client.MultiScan(spec.DefnId, requestID, spec.Scans, false, false, nil, 0, spec.Limit,
			cons, nil, callb)
	}

	if err != nil {
		errFn(err.Error())
	}

	dur := time.Now().Sub(startTime)

	if result != nil {
		aggrQ <- &JobResult{
			job:  job,
			dur:  dur.Nanoseconds(),
			rows: rows,
		}
	}
}

func Worker(jobQ chan *Job, c *qclient.GsiClient, aggrQ chan *JobResult, wg *sync.WaitGroup) {
	defer wg.Done()

	for job := range jobQ {
		RunJob(c, job, aggrQ)
	}
}

func ResultAggregator(ch chan *JobResult, sw io.Writer, wg *sync.WaitGroup) {
	defer wg.Done()

	for jr := range ch {
		var lat int64
		result := jr.job.result
		spec := jr.job.spec
		result.Rows += uint64(jr.rows)
		result.Duration += jr.dur

		result.statsRows += uint64(jr.rows)
		result.statsDuration += jr.dur

		if jr.rows > 0 {
			lat = jr.dur / jr.rows
		}
		result.LatencyHisto.Add(lat)

		result.iter++
		if sw != nil && spec.NInterval > 0 &&
			(result.iter%spec.NInterval == 0 || result.iter == spec.Repeat+1) {
			fmt.Fprintf(sw, "id:%d, rows:%d, duration:%d, Nth-latency:%d\n",
				spec.Id, result.statsRows, result.statsDuration, jr.dur)
			result.statsRows = 0
			result.statsDuration = 0
		}
	}
}

func RunCommands(cluster string, cfg *Config, statsW io.Writer) (*Result, error) {
	t0 := time.Now()
	var result Result

	var clients []*qclient.GsiClient
	var jobQ chan *Job
	var aggrQ chan *JobResult
	var wg1, wg2 sync.WaitGroup

	if len(cfg.LatencyBuckets) == 0 {
		cfg.LatencyBuckets = defaultLatencyBuckets
	}

	if cfg.ClientBootTime == 0 {
		cfg.ClientBootTime = clientBootTime
	}

	Init()

	config := c.SystemConfig.SectionConfig("queryport.client.", true)
	config.SetValue("settings.poolSize", int(cfg.Concurrency))
	config.SetValue("readDeadline", 0)
	config.SetValue("writeDeadline", 0)

	client, err := qclient.NewGsiClient(cluster, config)
	if err != nil {
		return nil, err
	}
	defer client.Close()

	clients = make([]*qclient.GsiClient, cfg.Clients)
	for i := 0; i < cfg.Clients; i++ {
		c, err := qclient.NewGsiClient(cluster, config)
		if err != nil {
			return nil, err
		}

		defer c.Close()
		clients[i] = c

	}

	time.Sleep(time.Second * time.Duration(cfg.ClientBootTime))
	indexes, _, _, err := client.Refresh()
	if err != nil {
		return nil, err
	}

	jobQ = make(chan *Job, cfg.Concurrency*1000)
	aggrQ = make(chan *JobResult, cfg.Concurrency*1000)
	for i := 0; i < cfg.Concurrency; i++ {
		wg1.Add(1)
		go Worker(jobQ, clients[i%cfg.Clients], aggrQ, &wg1)
	}

	wg2.Add(1)
	go ResultAggregator(aggrQ, statsW, &wg2)

	for i, spec := range cfg.ScanSpecs {
		if spec.Id == 0 {
			spec.Id = uint64(i)
		}

		for _, index := range indexes {
			if index.Definition.Bucket == spec.Bucket &&
				index.Definition.Name == spec.Index {
				spec.DefnId = uint64(index.Definition.DefnId)
			}
		}

		hFn := func(v int64) string {
			if v == math.MinInt64 {
				return "0"
			} else if v == math.MaxInt64 {
				return "inf"
			}
			return fmt.Sprint(time.Nanosecond * time.Duration(v))
		}

		res := new(ScanResult)
		res.ErrorCount = 0
		res.LatencyHisto.Init(cfg.LatencyBuckets, hFn)
		res.Id = spec.Id
		result.ScanResults = append(result.ScanResults, res)
	}

	// warming up GsiClient
	for _, client := range clients {
		for _, spec := range cfg.ScanSpecs {
			job := &Job{spec: spec, result: nil}
			RunJob(client, job, nil)
			break
		}
	}

	fmt.Println("GsiClients warmed up ...")
	result.WarmupDuration = float64(time.Since(t0).Nanoseconds()) / float64(time.Second)

	// Round robin scheduling of jobs
	var allFinished bool

loop:
	for {
		allFinished = true
		for i, spec := range cfg.ScanSpecs {
			if iter := atomic.LoadUint32(&spec.iteration); iter < spec.Repeat+1 {
				j := &Job{
					spec:   spec,
					result: result.ScanResults[i],
				}

				jobQ <- j
				atomic.AddUint32(&spec.iteration, 1)
				allFinished = false
			}
		}

		if allFinished {
			break loop
		}
	}

	close(jobQ)
	wg1.Wait()
	close(aggrQ)
	wg2.Wait()

	return &result, err
}
