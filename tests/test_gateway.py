import json
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from genai_gateway.core import Gateway, RetryableProviderError, FatalProviderError
from genai_gateway.server import GatewayHTTPServer


class Clock:
    def __init__(self): self.t = 0.0; self.waits = []
    def now(self): return self.t
    def sleep(self, delay): self.waits.append(delay); self.t += delay
    def forward(self, s): self.t += s


class ScriptedProvider:
    def __init__(self, actions): self.actions=list(actions); self.calls=0
    def generate(self, prompt):
        self.calls+=1
        action=self.actions.pop(0)
        if isinstance(action, Exception): raise action
        return action


class GatewayTests(unittest.TestCase):
    def test_mock_happy_path_and_no_echo(self):
        gw=Gateway()
        response=gw.generate('Synthetic support query')
        self.assertEqual(response['status'],200)
        self.assertNotIn('Synthetic support query',json.dumps(response))

    def test_retryable_error_recovers(self):
        c=Clock();p=ScriptedProvider([RetryableProviderError('quota',0.5),'ok'])
        g=Gateway(p,now=c.now,sleep=c.sleep)
        r=g.generate('ping')
        self.assertEqual((r['attempts'],r['text']), (2, 'ok'))
        self.assertEqual(c.waits,[0.5])
        self.assertEqual(g.metrics()['retries'],1)

    def test_fatal_errors_do_not_retry(self):
        p=ScriptedProvider([FatalProviderError('bad'), 'should-not-run'])
        g=Gateway(p)
        self.assertEqual(g.generate('ping')['error'],'provider_rejected')
        self.assertEqual(p.calls,1)

    def test_circuit_opens_and_recovers_after_cooldown(self):
        c=Clock();p=ScriptedProvider([RetryableProviderError('failed')]*6 + ['recovered'])
        g=Gateway(p,now=c.now,sleep=c.sleep,max_attempts=3,breaker_threshold=2,breaker_cooldown=5)
        self.assertEqual(g.generate('first')['status'],503)
        self.assertEqual(g.generate('second')['status'],503)
        self.assertEqual(g.generate('blocked')['error'],'circuit_open')
        self.assertEqual(p.calls,6)
        c.forward(5)
        self.assertEqual(g.generate('probe')['text'],'recovered')
        self.assertEqual(g.metrics()['circuit_state'],'closed')

    def test_token_bucket(self):
        c=Clock();g=Gateway(capacity=2,refill_per_second=1,now=c.now,sleep=c.sleep)
        self.assertEqual(g.generate('a')['status'],200)
        self.assertEqual(g.generate('b')['status'],200)
        self.assertEqual(g.generate('c')['status'],429)
        c.forward(1)
        self.assertEqual(g.generate('d')['status'],200)

    def test_per_client_budget(self):
        c=Clock();g=Gateway(capacity=1,now=c.now,sleep=c.sleep)
        self.assertEqual(g.generate('p',client='one')['status'],200)
        self.assertEqual(g.generate('p',client='two')['status'],200)
        self.assertEqual(g.generate('p',client='one')['status'],429)

    def test_idempotent_replay_and_conflict(self):
        p=ScriptedProvider(['hello'])
        g=Gateway(p)
        a=g.generate('same',idempotency_key='id-123')
        b=g.generate('same',idempotency_key='id-123')
        c=g.generate('different',idempotency_key='id-123')
        self.assertEqual(a['request_id'], b['request_id'])
        self.assertTrue(b['cached'])
        self.assertEqual(c['status'],409)
        self.assertEqual(p.calls,1)

    def test_idempotency_expires(self):
        c=Clock();p=ScriptedProvider(['first','second']);g=Gateway(p,now=c.now,sleep=c.sleep,cache_ttl=2)
        g.generate('a',idempotency_key='x')
        c.forward(3)
        self.assertEqual(g.generate('a',idempotency_key='x')['text'],'second')

    def test_request_validation(self):
        g=Gateway()
        self.assertEqual(g.generate('')['status'],400)
        self.assertEqual(g.generate('hello',client='not valid')['status'],400)
        self.assertEqual(g.generate('a'*2001)['status'],400)
        self.assertEqual(g.generate('hello',idempotency_key='bad value')['status'],400)

    def test_provider_exception_is_redacted(self):
        p=ScriptedProvider([RuntimeError('secret-api-token=please-do-not-log')])
        g=Gateway(p)
        self.assertNotIn('secret-api-token',json.dumps(g.generate('test')))

    def test_backoff_is_bounded(self):
        c=Clock();p=ScriptedProvider([RetryableProviderError('quota',999), 'ok'])
        g=Gateway(p,now=c.now,sleep=c.sleep)
        g.generate('test')
        self.assertEqual(c.waits,[1.0])

    def test_invalid_network_binding_rejected(self):
        with self.assertRaises(ValueError):
            GatewayHTTPServer(('0.0.0.0',0))


class HTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=GatewayHTTPServer(('127.0.0.1',0),Gateway())
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)

    def test_health_and_metrics(self):
        with urlopen(self.url+'/health',timeout=2) as response:
            self.assertTrue(json.load(response)['local_only'])
        with urlopen(self.url+'/metrics',timeout=2) as response:
            self.assertIn('circuit_state',json.load(response))

    def test_generate_api(self):
        payload=json.dumps({'prompt':'test'}).encode()
        req=Request(self.url+'/generate',data=payload,headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=2) as response:
            self.assertEqual(json.load(response)['status'],200)

    def test_large_body_is_rejected(self):
        req=Request(self.url+'/generate',data=b'a'*9000)
        with self.assertRaises(HTTPError) as caught:
            urlopen(req,timeout=2)
        self.assertEqual(caught.exception.code,413)


if __name__=='__main__':unittest.main()
