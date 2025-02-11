# Licensed under the MIT License
# https://github.com/craigahobbs/unittest-parallel/blob/main/LICENSE

"""
unittest-parallel command-line script main module
"""

import argparse
from contextlib import contextmanager
from io import StringIO
import multiprocessing
import os
import sys
import tempfile
import time
import unittest

import coverage


def main(argv=None):
    """
    unittest-parallel command-line script main entry point
    """

    # Command line arguments
    parser = argparse.ArgumentParser(prog='unittest-parallel')
    parser.add_argument('-v', '--verbose', action='store_const', const=2, default=1,
                        help='Verbose output')
    parser.add_argument('-q', '--quiet', dest='verbose', action='store_const', const=0, default=1,
                        help='Quiet output')
    parser.add_argument('-f', '--failfast', action='store_true', default=False,
                        help='Stop on first fail or error')
    parser.add_argument('-b', '--buffer', action='store_true', default=False,
                        help='Buffer stdout and stderr during tests')
    parser.add_argument('-j', '--jobs', metavar='COUNT', type=int, default=0,
                        help='The number of test processes (default is 0, all cores)')
    parser.add_argument('--class-fixtures', action='store_true', default=False,
                        help='One or more TestCase class has a setUpClass method')
    parser.add_argument('--module-fixtures', action='store_true', default=False,
                        help='One or more test module has a setUpModule method')
    group_unittest = parser.add_argument_group('unittest options')
    group_unittest.add_argument('-s', '--start-directory', metavar='START', default='.',
                                help="Directory to start discovery ('.' default)")
    group_unittest.add_argument('-p', '--pattern', metavar='PATTERN', default='test*.py',
                                help="Pattern to match tests ('test*.py' default)")
    group_unittest.add_argument('-t', '--top-level-directory', metavar='TOP',
                                help='Top level directory of project (defaults to start directory)')
    group_coverage = parser.add_argument_group('coverage options')
    group_coverage.add_argument('--coverage', action='store_true',
                                help='Run tests with coverage')
    group_coverage.add_argument('--coverage-branch', action='store_true',
                                help='Run tests with branch coverage')
    group_coverage.add_argument('--coverage-rcfile', metavar='RCFILE',
                                help='Specify coverage configuration file')
    group_coverage.add_argument('--coverage-include', metavar='PAT', action='append',
                                help='Include only files matching one of these patterns. Accepts shell-style (quoted) wildcards.')
    group_coverage.add_argument('--coverage-omit', metavar='PAT', action='append',
                                help='Omit files matching one of these patterns. Accepts shell-style (quoted) wildcards.')
    group_coverage.add_argument('--coverage-source', metavar='SRC', action='append',
                                help='A list of packages or directories of code to be measured')
    group_coverage.add_argument('--coverage-html', metavar='DIR',
                                help='Generate coverage HTML report')
    group_coverage.add_argument('--coverage-xml', metavar='FILE',
                                help='Generate coverage XML report')
    group_coverage.add_argument('--coverage-fail-under', metavar='MIN', type=float,
                                help='Fail if coverage percentage under min')
    args = parser.parse_args(args=argv)
    if args.coverage_branch:
        args.coverage = args.coverage_branch

    process_count = max(0, args.jobs)
    if process_count == 0:
        process_count = multiprocessing.cpu_count()

    # Create the temporary directory (for coverage files)
    with tempfile.TemporaryDirectory() as temp_dir:

        # Discover tests
        with _coverage(args, temp_dir):
            test_loader = unittest.TestLoader()
            discover_suite = test_loader.discover(args.start_directory, pattern=args.pattern, top_level_dir=args.top_level_directory)

        # Get the parallelizable test suites
        if args.module_fixtures:
            test_suites = list(_iter_module_suites(discover_suite))
        elif args.class_fixtures:
            test_suites = list(_iter_class_suites(discover_suite))
        else:
            test_suites = list(_iter_test_cases(discover_suite))

        # Don't use more processes than test suites
        process_count = max(1, min(len(test_suites), process_count))

        # Report test suites and processes
        print(
            f'Running {len(test_suites)} test suites ({discover_suite.countTestCases()} total tests) across {process_count} processes',
            file=sys.stderr
        )
        if args.verbose > 1:
            print(file=sys.stderr)

        # Run the tests in parallel
        start_time = time.perf_counter()
        with multiprocessing.Pool(process_count) as pool, multiprocessing.Manager() as manager:
            test_manager = ParallelTestManager(manager, args, temp_dir)
            results = pool.map(test_manager.run_tests, test_suites)
        stop_time = time.perf_counter()
        test_duration = stop_time - start_time

        # Aggregate parallel test run results
        tests_run = 0
        errors = []
        failures = []
        skipped = 0
        expected_failures = 0
        unexpected_successes = 0
        for result in results:
            tests_run += result[0]
            errors.extend(result[1])
            failures.extend(result[2])
            skipped += result[3]
            expected_failures += result[4]
            unexpected_successes += result[5]
        is_success = not(errors or failures or unexpected_successes)

        # Compute test info
        infos = []
        if failures:
            infos.append(f'failures={len(failures)}')
        if errors:
            infos.append(f'errors={len(errors)}')
        if skipped:
            infos.append(f'skipped={skipped}')
        if expected_failures:
            infos.append(f'expected failures={expected_failures}')
        if unexpected_successes:
            infos.append(f'unexpected successes={unexpected_successes}')

        # Report test errors
        if errors or failures:
            print(file=sys.stderr)
            for error in errors:
                print(error, file=sys.stderr)
            for failure in failures:
                print(failure, file=sys.stderr)
        elif args.verbose > 0:
            print(file=sys.stderr)

        # Test report
        print(unittest.TextTestResult.separator2, file=sys.stderr)
        print(f'Ran {tests_run} {"tests" if tests_run > 1 else "test"} in {test_duration:.3f}s', file=sys.stderr)
        print(file=sys.stderr)
        print(f'{"OK" if is_success else "FAILED"}{" (" + ", ".join(infos) + ")" if infos else ""}', file=sys.stderr)

        # Return an error status on failure
        if not is_success:
            parser.exit(status=len(errors) + len(failures) + unexpected_successes)

        # Coverage?
        if args.coverage:

            # Combine the coverage files
            cov = coverage.Coverage(config_file=args.coverage_rcfile)
            cov.combine(data_paths=[os.path.join(temp_dir, x) for x in os.listdir(temp_dir)])

            # Coverage report
            print(file=sys.stderr)
            percent_covered = cov.report(ignore_errors=True, file=sys.stderr)
            print(file=sys.stderr)
            print(f'Total coverage is {percent_covered:.2f}%', file=sys.stderr)

            # HTML coverage report
            if args.coverage_html:
                cov.html_report(directory=args.coverage_html, ignore_errors=True)

            # XML coverage report
            if args.coverage_xml:
                cov.xml_report(outfile=args.coverage_xml, ignore_errors=True)

            # Fail under
            if args.coverage_fail_under and percent_covered < args.coverage_fail_under:
                parser.exit(status=2)


@contextmanager
def _coverage(args, temp_dir):
    # Running tests with coverage?
    if args.coverage:
        # Generate a random coverage data file name - file is deleted along with containing directory
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as coverage_file:
            pass

        # Create the coverage object
        cov = coverage.Coverage(
            config_file=args.coverage_rcfile,
            data_file=coverage_file.name,
            branch=args.coverage_branch,
            include=args.coverage_include,
            omit=(args.coverage_omit if args.coverage_omit else []) + [__file__],
            source=args.coverage_source
        )
        try:
            # Start measuring code coverage
            cov.start()

            # Yield for unit test running
            yield cov
        finally:
            # Stop measuring code coverage
            cov.stop()

            # Save the collected coverage data to the data file
            cov.save()
    else:
        # Not running tests with coverage - yield for unit test running
        yield None


# Iterate module-level test suites - all top-level test suites returned from TestLoader.discover
def _iter_module_suites(test_suite):
    for module_suite in test_suite:
        if module_suite.countTestCases():
            yield module_suite


# Iterate class-level test suites - test suites that contains test cases
def _iter_class_suites(test_suite):
    has_cases = any(isinstance(suite, unittest.TestCase) for suite in test_suite)
    if has_cases:
        yield test_suite
    else:
        for suite in test_suite:
            yield from _iter_class_suites(suite)


# Iterate test cases (methods)
def _iter_test_cases(test_suite):
    if isinstance(test_suite, unittest.TestCase):
        yield test_suite
    else:
        for suite in test_suite:
            yield from _iter_test_cases(suite)


class ParallelTestManager:

    def __init__(self, manager, args, temp_dir):
        self.args = args
        self.temp_dir = temp_dir
        self.failfast = manager.Event()

    def run_tests(self, test_suite):
        # Fail fast?
        if self.failfast.is_set():
            return [0, [], [], 0, 0, 0]

        # Run unit tests
        with _coverage(self.args, self.temp_dir):
            runner = unittest.TextTestRunner(
                stream=StringIO(),
                resultclass=ParallelTextTestResult,
                verbosity=self.args.verbose,
                failfast=self.args.failfast,
                buffer=self.args.buffer
            )
            result = runner.run(test_suite)

            # Set failfast, if necessary
            if result.shouldStop:
                self.failfast.set()

            # Return (test_count, errors, failures, skipped_count, expected_failure_count, unexpected_success_count)
            return (
                result.testsRun,
                [self._format_error(result, error) for error in result.errors],
                [self._format_error(result, failure) for failure in result.failures],
                len(result.skipped),
                len(result.expectedFailures),
                len(result.unexpectedSuccesses)
            )

    @staticmethod
    def _format_error(result, error):
        return '\n'.join([
            unittest.TextTestResult.separator1,
            result.getDescription(error[0]),
            unittest.TextTestResult.separator2,
            error[1]
        ])


class ParallelTextTestResult(unittest.TextTestResult):

    def __init__(self, stream, descriptions, verbosity):
        stream = type(stream)(sys.stderr)
        super().__init__(stream, descriptions, verbosity)

    def startTest(self, test):
        if self.showAll:
            self.stream.writeln(f'{self.getDescription(test)} ...')
            self.stream.flush()
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).startTest(test)

    def _add_helper(self, test, dots_message, show_all_message):
        if self.showAll:
            self.stream.writeln(f'{self.getDescription(test)} ... {show_all_message}')
        elif self.dots:
            self.stream.write(dots_message)
        self.stream.flush()

    def addSuccess(self, test):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addSuccess(test)
        self._add_helper(test, '.', 'ok')

    def addError(self, test, err):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addError(test, err)
        self._add_helper(test, 'E', 'ERROR')

    def addFailure(self, test, err):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addFailure(test, err)
        self._add_helper(test, 'F', 'FAIL')

    def addSkip(self, test, reason):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addSkip(test, reason)
        self._add_helper(test, 's', f'skipped {reason!r}')

    def addExpectedFailure(self, test, err):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addExpectedFailure(test, err)
        self._add_helper(test, 'x', 'expected failure')

    def addUnexpectedSuccess(self, test):
        # pylint: disable=bad-super-call
        super(unittest.TextTestResult, self).addUnexpectedSuccess(test)
        self._add_helper(test, 'u', 'unexpected success')

    def printErrors(self):
        pass
