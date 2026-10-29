// HTTP load test for POST /v1/check, run with k6 (https://k6.io).
//
//   k6 run -e ALGORITHM=GCRA -e RATE=1000 benchmarks/check.js
//
// Options (all optional):
//   BASE_URL    default http://localhost:8000
//   ALGORITHM   FIXED_WINDOW | SLIDING_WINDOW_LOG | SLIDING_WINDOW_COUNTER | GCRA (default GCRA)
//   RATE        requests per second to send (default 1000)
//   DURATION    default 60s
//   IDENTITIES  how many different users to spread requests over (default 10000)
//
// "constant-arrival-rate" starts requests on a fixed schedule, whether or not
// earlier ones have finished. Tools that wait for each response before sending
// the next one quietly send less when the server slows down, which hides the
// slowness ("coordinated omission"). This doesn't.

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const ALGORITHM = __ENV.ALGORITHM || 'GCRA';
const RATE = Number(__ENV.RATE || 1000);
const DURATION = __ENV.DURATION || '60s';
const IDENTITIES = Number(__ENV.IDENTITIES || 10000);
const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

export const options = {
 scenarios: {
   checks: {
     executor: 'constant-arrival-rate',
     rate: RATE,
     timeUnit: '1s',
     duration: DURATION,
     preAllocatedVUs: 200,
     maxVUs: 2000,
   },
 },
 summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
 thresholds: {
   http_req_failed: ['rate<0.01'], // fewer than 1% errors, or the run counts as failed
 },
};

// Runs once before the test: create (or update) the policy being tested.
export function setup() {
 const policy = 'bench-' + ALGORITHM.toLowerCase().split('_').join('-');
 const body = JSON.stringify({ algorithm: ALGORITHM, bands: [{ limit: 100, window: 'PT1S' }] });
 const response = http.put(`${BASE_URL}/v1/policies/${policy}`, body, JSON_HEADERS);
 check(response, { 'policy saved': (r) => r.status === 200 || r.status === 201 });
 return { policy };
}

export default function (data) {
 const identity = `user-${Math.floor(Math.random() * IDENTITIES)}`;
 const body = JSON.stringify({ policy: data.policy, identity });
 const response = http.post(`${BASE_URL}/v1/check`, body, JSON_HEADERS);
 check(response, {
   'status 200': (r) => r.status === 200,
   'decided by Redis (not degraded)': (r) => r.status === 200 && r.json('degraded') === false,
 });
}
 