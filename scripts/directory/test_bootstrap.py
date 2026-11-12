import copy
import unittest
import bootstrap


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'version': 1, 'dns_domain': 'office.smolofis.internal', 'short_domain': 'SMOLOFIS',
                     'hostname': 'smolofis-dc', 'address': '192.168.1.17', 'forwarder': None}

    def test_explicit_private_plan_and_stable_digest(self):
        self.assertEqual(bootstrap.validate_plan(self.plan), self.plan)
        self.assertEqual(bootstrap.plan_digest(self.plan), bootstrap.plan_digest(dict(reversed(list(self.plan.items())))))

    def test_names_reject_injection_mdns_and_domain_host_collision(self):
        for key, value in [('dns_domain', 'office.local'), ('dns_domain', 'office..internal'),
                           ('dns_domain', 'Office.internal'), ('dns_domain', 'office.internal\npath=x'),
                           ('short_domain', 'OFFICE;id'), ('short_domain', 'A'*16),
                           ('hostname', 'smolofis'), ('hostname', '../escape')]:
            plan = dict(self.plan, **{key:value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                bootstrap.validate_plan(plan)

    def test_address_and_forwarder_validation(self):
        for key, value in [('address', '127.0.0.1'), ('address', '8.8.8.8'), ('address', '::1'),
                           ('forwarder', '192.168.1.17'), ('forwarder', '127.0.0.1'),
                           ('forwarder', '224.0.0.1'), ('forwarder', '0.0.0.0')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                bootstrap.validate_plan(dict(self.plan, **{key:value}))
        self.assertEqual(bootstrap.validate_plan(dict(self.plan, forwarder='192.168.1.1'))['forwarder'], '192.168.1.1')

    def test_fields_and_duplicate_keys_are_strict(self):
        for change in ({'version': True}, {'version': 2}, {'address': 123}):
            with self.assertRaises(ValueError): bootstrap.validate_plan(dict(self.plan, **change))
        with self.assertRaises(ValueError): bootstrap.validate_plan(dict(self.plan, command='id'))
        plan = copy.copy(self.plan); del plan['forwarder']
        with self.assertRaises(ValueError): bootstrap.validate_plan(plan)
        with self.assertRaises(ValueError): bootstrap.unique_object([('version',1),('version',2)])

    def test_pending_state_is_never_reported_prepared_or_started(self):
        report = bootstrap.summary({'operation_id':'ab'*32,'phase':'preparing'})
        self.assertEqual(report['phase'],'interrupted')
        self.assertFalse(report['service_started_by_command'])
        self.assertFalse(report['dns_changed_by_command'])


if __name__ == '__main__':
    unittest.main()
