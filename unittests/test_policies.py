# Copyright 2016-2020 Swiss National Supercomputing Centre (CSCS/ETH Zurich)
# ReFrame Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: BSD-3-Clause

import collections
import itertools
import os
import pytest
import time
import tempfile
import unittest

import reframe as rfm
import reframe.core.runtime as rt
import reframe.frontend.dependency as dependency
import reframe.frontend.executors as executors
import reframe.frontend.executors.policies as policies
import reframe.utility as util
import reframe.utility.os_ext as os_ext
from reframe.core.environments import Environment
from reframe.core.exceptions import (
    DependencyError, JobNotStartedError,
    ReframeForceExitError, TaskDependencyError
)
from reframe.frontend.loader import RegressionCheckLoader
import unittests.fixtures as fixtures
from unittests.resources.checks.hellocheck import HelloTest
from unittests.resources.checks.frontend_checks import (
    BadSetupCheck,
    BadSetupCheckEarly,
    KeyboardInterruptCheck,
    RetriesCheck,
    SleepCheck,
    SleepCheckPollFail,
    SleepCheckPollFailLate,
    SystemExitCheck,
)


class TestSerialExecutionPolicy(unittest.TestCase):
    def setUp(self):
        self.loader = RegressionCheckLoader(['unittests/resources/checks'],
                                            ignore_conflicts=True)

        # Setup the runner
        self.runner = executors.Runner(policies.SerialExecutionPolicy())
        self.checks = self.loader.load_all()

        # Set runtime prefix
        rt.runtime().resources.prefix = tempfile.mkdtemp(dir='unittests')

        # Reset current_run
        rt.runtime()._current_run = 0

    def tearDown(self):
        os_ext.rmtree(rt.runtime().resources.prefix)

    def runall(self, checks, sort=False, *args, **kwargs):
        cases = executors.generate_testcases(checks, *args, **kwargs)
        if sort:
            depgraph = dependency.build_deps(cases)
            dependency.validate_deps(depgraph)
            cases = dependency.toposort(depgraph)

        self.runner.runall(cases)

    def assertRunall(self):
        # Make sure that all cases finished or failed
        for t in self.runner.stats.tasks():
            assert t.succeeded or t.failed

    def _num_failures_stage(self, stage):
        stats = self.runner.stats
        return len([t for t in stats.failures() if t.failed_stage == stage])

    def assert_all_dead(self):
        stats = self.runner.stats
        for t in self.runner.stats.tasks():
            try:
                finished = t.check.poll()
            except JobNotStartedError:
                finished = True

            assert finished

    def test_runall(self):
        self.runall(self.checks)

        stats = self.runner.stats
        assert 8 == stats.num_cases()
        self.assertRunall()
        assert 5 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 1 == self._num_failures_stage('sanity')
        assert 1 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_runall_skip_system_check(self):
        self.runall(self.checks, skip_system_check=True)

        stats = self.runner.stats
        assert 9 == stats.num_cases()
        self.assertRunall()
        assert 5 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 1 == self._num_failures_stage('sanity')
        assert 1 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_runall_skip_prgenv_check(self):
        self.runall(self.checks, skip_environ_check=True)

        stats = self.runner.stats
        assert 9 == stats.num_cases()
        self.assertRunall()
        assert 5 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 1 == self._num_failures_stage('sanity')
        assert 1 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_runall_skip_sanity_check(self):
        self.runner.policy.skip_sanity_check = True
        self.runall(self.checks)

        stats = self.runner.stats
        assert 8 == stats.num_cases()
        self.assertRunall()
        assert 4 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 0 == self._num_failures_stage('sanity')
        assert 1 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_runall_skip_performance_check(self):
        self.runner.policy.skip_performance_check = True
        self.runall(self.checks)

        stats = self.runner.stats
        assert 8 == stats.num_cases()
        self.assertRunall()
        assert 4 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 1 == self._num_failures_stage('sanity')
        assert 0 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_strict_performance_check(self):
        self.runner.policy.strict_check = True
        self.runall(self.checks)

        stats = self.runner.stats
        assert 8 == stats.num_cases()
        self.assertRunall()
        assert 6 == len(stats.failures())
        assert 2 == self._num_failures_stage('setup')
        assert 1 == self._num_failures_stage('sanity')
        assert 2 == self._num_failures_stage('performance')
        assert 1 == self._num_failures_stage('cleanup')

    def test_force_local_execution(self):
        self.runner.policy.force_local = True
        self.runall([HelloTest()])
        self.assertRunall()
        stats = self.runner.stats
        for t in stats.tasks():
            assert t.check.local

    def test_kbd_interrupt_within_test(self):
        check = KeyboardInterruptCheck()
        with pytest.raises(KeyboardInterrupt):
            self.runall([check])

        stats = self.runner.stats
        assert 1 == len(stats.failures())
        self.assert_all_dead()

    def test_system_exit_within_test(self):
        check = SystemExitCheck()

        # This should not raise and should not exit
        self.runall([check])
        stats = self.runner.stats
        assert 1 == len(stats.failures())

    def test_retries_bad_check(self):
        max_retries = 2
        checks = [BadSetupCheck(), BadSetupCheckEarly()]
        self.runner._max_retries = max_retries
        self.runall(checks)

        # Ensure that the test was retried #max_retries times and failed.
        assert 2 == self.runner.stats.num_cases()
        self.assertRunall()
        assert max_retries == rt.runtime().current_run
        assert 2 == len(self.runner.stats.failures())

        # Ensure that the report does not raise any exception.
        self.runner.stats.retry_report()

    def test_retries_good_check(self):
        max_retries = 2
        checks = [HelloTest()]
        self.runner._max_retries = max_retries
        self.runall(checks)

        # Ensure that the test passed without retries.
        assert 1 == self.runner.stats.num_cases()
        self.assertRunall()
        assert 0 == rt.runtime().current_run
        assert 0 == len(self.runner.stats.failures())

    def test_pass_in_retries(self):
        max_retries = 3
        run_to_pass = 2
        # Create a file containing the current_run; Run 0 will set it to 0,
        # run 1 to 1 and so on.
        with tempfile.NamedTemporaryFile(mode='wt', delete=False) as fp:
            fp.write('0\n')

        checks = [RetriesCheck(run_to_pass, fp.name)]
        self.runner._max_retries = max_retries
        self.runall(checks)

        # Ensure that the test passed after retries in run #run_to_pass.
        assert 1 == self.runner.stats.num_cases()
        self.assertRunall()
        assert 1 == len(self.runner.stats.failures(run=0))
        assert run_to_pass == rt.runtime().current_run
        assert 0 == len(self.runner.stats.failures())
        os.remove(fp.name)

    def test_dependencies(self):
        self.loader = RegressionCheckLoader(
            ['unittests/resources/checks_unlisted/deps_complex.py']
        )

        # Setup the runner
        self.checks = self.loader.load_all()
        self.runall(self.checks, sort=True)

        self.assertRunall()
        stats = self.runner.stats
        assert stats.num_cases(0) == 10
        assert len(stats.failures()) == 4
        for tf in stats.failures():
            check = tf.testcase.check
            _, exc_value, _ = tf.exc_info
            if check.name == 'T7' or check.name == 'T9':
                assert isinstance(exc_value, TaskDependencyError)

        # Check that cleanup is executed properly for successful tests as well
        for t in stats.tasks():
            check = t.testcase.check
            if t.failed:
                continue

            if t.ref_count == 0:
                assert os.path.exists(os.path.join(check.outputdir, 'out.txt'))

    def test_sigterm(self):
        self.loader = RegressionCheckLoader(
            ['unittests/resources/checks_unlisted/selfkill.py']
        )
        checks = self.loader.load_all()
        with pytest.raises(ReframeForceExitError,
                           match='received TERM signal'):
            self.runall(checks)

        self.assert_all_dead()
        assert self.runner.stats.num_cases() == 1
        assert len(self.runner.stats.failures()) == 1

    def test_dependencies_with_retries(self):
        self.runner._max_retries = 2
        self.test_dependencies()


class TaskEventMonitor(executors.TaskEventListener):
    '''Event listener for monitoring the execution of the asynchronous
    execution policy.

    We need to make sure two things for the async policy:

    1. The number of running tasks never exceed the max job size per partition.
    2. Given a set of regression tests with a reasonably long runtime, the
       execution policy must be able to reach the maximum concurrency. By
       reasonably long runtime, we mean that that the regression tests must run
       enough time, so as to allow the policy to execute all the tests until
       their "run" phase, before the first submitted test finishes.
    '''

    def __init__(self):
        super().__init__()

        # timeline of num_tasks
        self.num_tasks = [0]
        self.tasks = []

    def on_task_run(self, task):
        super().on_task_run(task)
        last = self.num_tasks[-1]
        self.num_tasks.append(last + 1)
        self.tasks.append(task)

    def on_task_exit(self, task):
        last = self.num_tasks[-1]
        self.num_tasks.append(last - 1)

    def on_task_success(self, task):
        pass

    def on_task_failure(self, task):
        pass

    def on_task_setup(self, task):
        pass


class TestAsynchronousExecutionPolicy(TestSerialExecutionPolicy):
    def setUp(self):
        super().setUp()
        self.runner = executors.Runner(policies.AsynchronousExecutionPolicy())
        self.runner.policy.keep_stage_files = True
        self.monitor = TaskEventMonitor()
        self.runner.policy.task_listeners.append(self.monitor)

    def set_max_jobs(self, value):
        for p in rt.runtime().system.partitions:
            p._max_jobs = value

    def read_timestamps(self, tasks):
        '''Read the timestamps and sort them to permit simple
        concurrency tests.'''
        from reframe.utility.sanity import evaluate

        self.begin_stamps = []
        self.end_stamps = []
        for t in tasks:
            with os_ext.change_dir(t.check.stagedir):
                with open(evaluate(t.check.stdout), 'r') as f:
                    self.begin_stamps.append(float(f.readline().strip()))
                    self.end_stamps.append(float(f.readline().strip()))

        self.begin_stamps.sort()
        self.end_stamps.sort()

    def test_concurrency_unlimited(self):
        checks = [SleepCheck(0.5) for i in range(3)]
        self.set_max_jobs(len(checks))
        self.runall(checks)

        # Ensure that all tests were run and without failures.
        assert len(checks) == self.runner.stats.num_cases()
        self.assertRunall()
        assert 0 == len(self.runner.stats.failures())

        # Ensure that maximum concurrency was reached as fast as possible
        assert len(checks) == max(self.monitor.num_tasks)
        assert len(checks) == self.monitor.num_tasks[len(checks)]

        self.read_timestamps(self.monitor.tasks)

        # Warn if not all tests were run in parallel; the corresponding strict
        # check would be:
        #
        #     self.assertTrue(self.begin_stamps[-1] <= self.end_stamps[0])
        #
        if self.begin_stamps[-1] > self.end_stamps[0]:
            pytest.skip('the system seems too much loaded.')

    def test_concurrency_limited(self):
        # The number of checks must be <= 2*max_jobs.
        checks = [SleepCheck(0.5) for i in range(5)]
        max_jobs = len(checks) - 2
        self.set_max_jobs(max_jobs)
        self.runall(checks)

        # Ensure that all tests were run and without failures.
        assert len(checks) == self.runner.stats.num_cases()
        self.assertRunall()
        assert 0 == len(self.runner.stats.failures())

        # Ensure that maximum concurrency was reached as fast as possible
        assert max_jobs == max(self.monitor.num_tasks)
        assert max_jobs == self.monitor.num_tasks[max_jobs]

        self.read_timestamps(self.monitor.tasks)

        # Ensure that the jobs after the first #max_jobs were each run after
        # one of the previous #max_jobs jobs had finished
        # (e.g. begin[max_jobs] > end[0]).
        # Note: we may ensure this strictly as we may ensure serial behaviour.
        begin_after_end = (b > e for b, e in zip(self.begin_stamps[max_jobs:],
                                                 self.end_stamps[:-max_jobs]))
        assert all(begin_after_end)

        # NOTE: to ensure that these remaining jobs were also run
        # in parallel one could do the command hereafter; however, it would
        # require to substantially increase the sleep time (in SleepCheck),
        # because of the delays in rescheduling (1s, 2s, 3s, 1s, 2s,...).
        # We currently prefer not to do this last concurrency test to avoid an
        # important prolongation of the unit test execution time.
        # self.assertTrue(self.begin_stamps[-1] < self.end_stamps[max_jobs])

        # Warn if the first #max_jobs jobs were not run in parallel; the
        # corresponding strict check would be:
        # self.assertTrue(self.begin_stamps[max_jobs-1] <= self.end_stamps[0])
        if self.begin_stamps[max_jobs-1] > self.end_stamps[0]:
            pytest.skip('the system seems too loaded.')

    def test_concurrency_none(self):
        checks = [SleepCheck(0.5) for i in range(3)]
        num_checks = len(checks)
        self.set_max_jobs(1)
        self.runall(checks)

        # Ensure that all tests were run and without failures.
        assert len(checks) == self.runner.stats.num_cases()
        self.assertRunall()
        assert 0 == len(self.runner.stats.failures())

        # Ensure that a single task was running all the time
        assert 1 == max(self.monitor.num_tasks)

        # Read the timestamps sorted to permit simple concurrency tests.
        self.read_timestamps(self.monitor.tasks)

        # Ensure that the jobs were run after the previous job had finished
        # (e.g. begin[1] > end[0]).
        begin_after_end = (b > e for b, e in zip(self.begin_stamps[1:],
                                                 self.end_stamps[:-1]))
        assert all(begin_after_end)

    def _run_checks(self, checks, max_jobs):
        self.set_max_jobs(max_jobs)
        with pytest.raises(KeyboardInterrupt):
            self.runall(checks)

        assert 4 == self.runner.stats.num_cases()
        self.assertRunall()
        assert 4 == len(self.runner.stats.failures())
        self.assert_all_dead()

    def test_kbd_interrupt_in_wait_with_concurrency(self):
        checks = [KeyboardInterruptCheck(),
                  SleepCheck(10), SleepCheck(10), SleepCheck(10)]
        self._run_checks(checks, 4)

    def test_kbd_interrupt_in_wait_with_limited_concurrency(self):
        # The general idea for this test is to allow enough time for all the
        # four checks to be submitted and at the same time we need the
        # KeyboardInterruptCheck to finish first (the corresponding wait should
        # trigger the failure), so as to make the framework kill the remaining
        # three.
        checks = [KeyboardInterruptCheck(),
                  SleepCheck(10), SleepCheck(10), SleepCheck(10)]
        self._run_checks(checks, 2)

    def test_kbd_interrupt_in_setup_with_concurrency(self):
        checks = [SleepCheck(1), SleepCheck(1), SleepCheck(1),
                  KeyboardInterruptCheck(phase='setup')]
        self._run_checks(checks, 4)

    def test_kbd_interrupt_in_setup_with_limited_concurrency(self):
        checks = [SleepCheck(1), SleepCheck(1), SleepCheck(1),
                  KeyboardInterruptCheck(phase='setup')]
        self._run_checks(checks, 2)

    def test_poll_fails_main_loop(self):
        num_tasks = 3
        checks = [SleepCheckPollFail(10) for i in range(num_tasks)]
        num_checks = len(checks)
        self.set_max_jobs(1)
        self.runall(checks)
        stats = self.runner.stats
        assert num_tasks == stats.num_cases()
        self.assertRunall()
        assert num_tasks == len(stats.failures())

    def test_poll_fails_busy_loop(self):
        num_tasks = 3
        checks = [SleepCheckPollFailLate(1/i) for i in range(1, num_tasks+1)]
        num_checks = len(checks)
        self.set_max_jobs(1)
        self.runall(checks)
        stats = self.runner.stats
        assert num_tasks == stats.num_cases()
        self.assertRunall()
        assert num_tasks == len(stats.failures())


class TestDependencies(unittest.TestCase):
    class Node:
        '''A node in the test case graph.

        It's simply a wrapper to a (test_name, partition, environment) tuple
        that can interact seemlessly with a real test case.
        It's meant for convenience in unit testing.
        '''

        def __init__(self, cname, pname, ename):
            self.cname, self.pname, self.ename = cname, pname, ename

        def __eq__(self, other):
            if isinstance(other, type(self)):
                return (self.cname == other.cname and
                        self.pname == other.pname and
                        self.ename == other.ename)

            if isinstance(other, executors.TestCase):
                return (self.cname == other.check.name and
                        self.pname == other.partition.fullname and
                        self.ename == other.environ.name)

            return NotImplemented

        def __hash__(self):
            return hash(self.cname) ^ hash(self.pname) ^ hash(self.ename)

        def __repr__(self):
            return 'Node(%r, %r, %r)' % (self.cname, self.pname, self.ename)

    def has_edge(graph, src, dst):
        return dst in graph[src]

    def num_deps(graph, cname):
        return sum(len(deps) for c, deps in graph.items()
                   if c.check.name == cname)

    def in_degree(graph, node):
        for v in graph.keys():
            if v == node:
                return v.num_dependents

    def find_check(name, checks):
        for c in checks:
            if c.name == name:
                return c

        return None

    def find_case(cname, ename, cases):
        for c in cases:
            if c.check.name == cname and c.environ.name == ename:
                return c

    def setUp(self):
        self.loader = RegressionCheckLoader([
            'unittests/resources/checks_unlisted/deps_simple.py'
        ])

        # Set runtime prefix
        rt.runtime().resources.prefix = tempfile.mkdtemp(dir='unittests')

    def tearDown(self):
        os_ext.rmtree(rt.runtime().resources.prefix)

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_eq_hash(self):
        find_case = TestDependencies.find_case
        cases = executors.generate_testcases(self.loader.load_all())

        case0 = find_case('Test0', 'e0', cases)
        case1 = find_case('Test0', 'e1', cases)
        case0_copy = case0.clone()

        assert case0 == case0_copy
        assert hash(case0) == hash(case0_copy)
        assert case1 != case0
        assert hash(case1) != hash(case0)

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_build_deps(self):
        Node = TestDependencies.Node
        has_edge = TestDependencies.has_edge
        num_deps = TestDependencies.num_deps
        in_degree = TestDependencies.in_degree
        find_check = TestDependencies.find_check
        find_case = TestDependencies.find_case

        checks = self.loader.load_all()
        cases = executors.generate_testcases(checks)

        # Test calling getdep() before having built the graph
        t = find_check('Test1_exact', checks)
        with pytest.raises(DependencyError):
            t.getdep('Test0', 'e0')

        # Build dependencies and continue testing
        deps = dependency.build_deps(cases)
        dependency.validate_deps(deps)

        # Check DEPEND_FULLY dependencies
        assert num_deps(deps, 'Test1_fully') == 8
        for p in ['sys0:p0', 'sys0:p1']:
            for e0 in ['e0', 'e1']:
                for e1 in ['e0', 'e1']:
                    assert has_edge(deps,
                                    Node('Test1_fully', p, e0),
                                    Node('Test0', p, e1))

        # Check DEPEND_BY_ENV
        assert num_deps(deps, 'Test1_by_env') == 4
        assert num_deps(deps, 'Test1_default') == 4
        for p in ['sys0:p0', 'sys0:p1']:
            for e in ['e0', 'e1']:
                assert has_edge(deps,
                                Node('Test1_by_env', p, e),
                                Node('Test0', p, e))
                assert has_edge(deps,
                                Node('Test1_default', p, e),
                                Node('Test0', p, e))

        # Check DEPEND_EXACT
        assert num_deps(deps, 'Test1_exact') == 6
        for p in ['sys0:p0', 'sys0:p1']:
            assert has_edge(deps,
                            Node('Test1_exact', p, 'e0'),
                            Node('Test0', p, 'e0'))
            assert has_edge(deps,
                            Node('Test1_exact', p, 'e0'),
                            Node('Test0', p, 'e1'))
            assert has_edge(deps,
                            Node('Test1_exact', p, 'e1'),
                            Node('Test0', p, 'e1'))

        # Check in-degree of Test0

        # 2 from Test1_fully,
        # 1 from Test1_by_env,
        # 1 from Test1_exact,
        # 1 from Test1_default
        assert in_degree(deps, Node('Test0', 'sys0:p0', 'e0')) == 5
        assert in_degree(deps, Node('Test0', 'sys0:p1', 'e0')) == 5

        # 2 from Test1_fully,
        # 1 from Test1_by_env,
        # 2 from Test1_exact,
        # 1 from Test1_default
        assert in_degree(deps, Node('Test0', 'sys0:p0', 'e1')) == 6
        assert in_degree(deps, Node('Test0', 'sys0:p1', 'e1')) == 6

        # Pick a check to test getdep()
        check_e0 = find_case('Test1_exact', 'e0', cases).check
        check_e1 = find_case('Test1_exact', 'e1', cases).check

        with pytest.raises(DependencyError):
            check_e0.getdep('Test0')

        # Set the current environment
        check_e0._current_environ = Environment('e0')
        check_e1._current_environ = Environment('e1')

        assert check_e0.getdep('Test0', 'e0').name == 'Test0'
        assert check_e0.getdep('Test0', 'e1').name == 'Test0'
        assert check_e1.getdep('Test0', 'e1').name == 'Test0'
        with pytest.raises(DependencyError):
            check_e0.getdep('TestX', 'e0')

        with pytest.raises(DependencyError):
            check_e0.getdep('Test0', 'eX')

        with pytest.raises(DependencyError):
            check_e1.getdep('Test0', 'e0')

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_build_deps_unknown_test(self):
        find_check = TestDependencies.find_check
        checks = self.loader.load_all()

        # Add some inexistent dependencies
        test0 = find_check('Test0', checks)
        for depkind in ('default', 'fully', 'by_env', 'exact'):
            test1 = find_check('Test1_' + depkind, checks)
            if depkind == 'default':
                test1.depends_on('TestX')
            elif depkind == 'exact':
                test1.depends_on('TestX', rfm.DEPEND_EXACT, {'e0': ['e0']})
            elif depkind == 'fully':
                test1.depends_on('TestX', rfm.DEPEND_FULLY)
            elif depkind == 'by_env':
                test1.depends_on('TestX', rfm.DEPEND_BY_ENV)

            with pytest.raises(DependencyError):
                dependency.build_deps(executors.generate_testcases(checks))

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_build_deps_unknown_target_env(self):
        find_check = TestDependencies.find_check
        checks = self.loader.load_all()

        # Add some inexistent dependencies
        test0 = find_check('Test0', checks)
        test1 = find_check('Test1_default', checks)
        test1.depends_on('Test0', rfm.DEPEND_EXACT, {'e0': ['eX']})
        with pytest.raises(DependencyError):
            dependency.build_deps(executors.generate_testcases(checks))

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_build_deps_unknown_source_env(self):
        find_check = TestDependencies.find_check
        num_deps = TestDependencies.num_deps
        checks = self.loader.load_all()

        # Add some inexistent dependencies
        test0 = find_check('Test0', checks)
        test1 = find_check('Test1_default', checks)
        test1.depends_on('Test0', rfm.DEPEND_EXACT, {'eX': ['e0']})

        # Unknown source is ignored, because it might simply be that the test
        # is not executed for eX
        deps = dependency.build_deps(executors.generate_testcases(checks))
        assert num_deps(deps, 'Test1_default') == 4

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_build_deps_empty(self):
        assert {} == dependency.build_deps([])

    def create_test(self, name):
        test = rfm.RegressionTest()
        test.name = name
        test.valid_systems = ['*']
        test.valid_prog_environs = ['*']
        test.executable = 'echo'
        test.executable_opts = [name]
        return test

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_valid_deps(self):
        #
        #       t0       +-->t5<--+
        #       ^        |        |
        #       |        |        |
        #   +-->t1<--+   t6       t7
        #   |        |            ^
        #   t2<------t3           |
        #   ^        ^            |
        #   |        |            t8
        #   +---t4---+
        #
        t0 = self.create_test('t0')
        t1 = self.create_test('t1')
        t2 = self.create_test('t2')
        t3 = self.create_test('t3')
        t4 = self.create_test('t4')
        t5 = self.create_test('t5')
        t6 = self.create_test('t6')
        t7 = self.create_test('t7')
        t8 = self.create_test('t8')
        t1.depends_on('t0')
        t2.depends_on('t1')
        t3.depends_on('t1')
        t3.depends_on('t2')
        t4.depends_on('t2')
        t4.depends_on('t3')
        t6.depends_on('t5')
        t7.depends_on('t5')
        t8.depends_on('t7')
        dependency.validate_deps(
            dependency.build_deps(
                executors.generate_testcases([t0, t1, t2, t3, t4,
                                              t5, t6, t7, t8])
            )
        )

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_cyclic_deps(self):
        #
        #       t0       +-->t5<--+
        #       ^        |        |
        #       |        |        |
        #   +-->t1<--+   t6       t7
        #   |   |    |            ^
        #   t2  |    t3           |
        #   ^   |    ^            |
        #   |   v    |            t8
        #   +---t4---+
        #
        t0 = self.create_test('t0')
        t1 = self.create_test('t1')
        t2 = self.create_test('t2')
        t3 = self.create_test('t3')
        t4 = self.create_test('t4')
        t5 = self.create_test('t5')
        t6 = self.create_test('t6')
        t7 = self.create_test('t7')
        t8 = self.create_test('t8')
        t1.depends_on('t0')
        t1.depends_on('t4')
        t2.depends_on('t1')
        t3.depends_on('t1')
        t4.depends_on('t2')
        t4.depends_on('t3')
        t6.depends_on('t5')
        t7.depends_on('t5')
        t8.depends_on('t7')
        deps = dependency.build_deps(
            executors.generate_testcases([t0, t1, t2, t3, t4,
                                          t5, t6, t7, t8])
        )

        with pytest.raises(DependencyError) as exc_info:
            dependency.validate_deps(deps)

        assert ('t4->t2->t1->t4' in str(exc_info.value) or
                't2->t1->t4->t2' in str(exc_info.value) or
                't1->t4->t2->t1' in str(exc_info.value) or
                't1->t4->t3->t1' in str(exc_info.value) or
                't4->t3->t1->t4' in str(exc_info.value) or
                't3->t1->t4->t3' in str(exc_info.value))

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_cyclic_deps_by_env(self):
        t0 = self.create_test('t0')
        t1 = self.create_test('t1')
        t1.depends_on('t0', rfm.DEPEND_EXACT, {'e0': ['e0']})
        t0.depends_on('t1', rfm.DEPEND_EXACT, {'e1': ['e1']})
        deps = dependency.build_deps(
            executors.generate_testcases([t0, t1])
        )
        with pytest.raises(DependencyError) as exc_info:
            dependency.validate_deps(deps)

        assert ('t1->t0->t1' in str(exc_info.value) or
                't0->t1->t0' in str(exc_info.value))

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_validate_deps_empty(self):
        dependency.validate_deps({})

    def assert_topological_order(self, cases, graph):
        cases_order = []
        visited_tests = set()
        tests = util.OrderedSet()
        for c in cases:
            check, part, env = c
            cases_order.append((check.name, part.fullname, env.name))
            tests.add(check.name)
            visited_tests.add(check.name)

            # Assert that all dependencies of c have been visited before
            for d in graph[c]:
                if d not in cases:
                    # dependency points outside the subgraph
                    continue

                assert d.check.name in visited_tests

        # Check the order of systems and prog. environments
        # We are checking against all possible orderings
        valid_orderings = []
        for partitions in itertools.permutations(['sys0:p0', 'sys0:p1']):
            for environs in itertools.permutations(['e0', 'e1']):
                ordering = []
                for t in tests:
                    for p in partitions:
                        for e in environs:
                            ordering.append((t, p, e))

                valid_orderings.append(ordering)

        assert cases_order in valid_orderings

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_toposort(self):
        #
        #       t0       +-->t5<--+
        #       ^        |        |
        #       |        |        |
        #   +-->t1<--+   t6       t7
        #   |        |            ^
        #   t2<------t3           |
        #   ^        ^            |
        #   |        |            t8
        #   +---t4---+
        #
        t0 = self.create_test('t0')
        t1 = self.create_test('t1')
        t2 = self.create_test('t2')
        t3 = self.create_test('t3')
        t4 = self.create_test('t4')
        t5 = self.create_test('t5')
        t6 = self.create_test('t6')
        t7 = self.create_test('t7')
        t8 = self.create_test('t8')
        t1.depends_on('t0')
        t2.depends_on('t1')
        t3.depends_on('t1')
        t3.depends_on('t2')
        t4.depends_on('t2')
        t4.depends_on('t3')
        t6.depends_on('t5')
        t7.depends_on('t5')
        t8.depends_on('t7')
        deps = dependency.build_deps(
            executors.generate_testcases([t0, t1, t2, t3, t4,
                                          t5, t6, t7, t8])
        )
        cases = dependency.toposort(deps)
        self.assert_topological_order(cases, deps)

    @rt.switch_runtime(fixtures.TEST_SITE_CONFIG, 'sys0')
    def test_toposort_subgraph(self):
        #
        #       t0
        #       ^
        #       |
        #   +-->t1<--+
        #   |        |
        #   t2<------t3
        #   ^        ^
        #   |        |
        #   +---t4---+
        #
        t0 = self.create_test('t0')
        t1 = self.create_test('t1')
        t2 = self.create_test('t2')
        t3 = self.create_test('t3')
        t4 = self.create_test('t4')
        t1.depends_on('t0')
        t2.depends_on('t1')
        t3.depends_on('t1')
        t3.depends_on('t2')
        t4.depends_on('t2')
        t4.depends_on('t3')
        full_deps = dependency.build_deps(
            executors.generate_testcases([t0, t1, t2, t3, t4])
        )
        partial_deps = dependency.build_deps(
            executors.generate_testcases([t3, t4]), full_deps
        )
        cases = dependency.toposort(partial_deps, is_subgraph=True)
        self.assert_topological_order(cases, partial_deps)
