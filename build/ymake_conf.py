#!/usr/bin/env python
# coding=utf-8

import base64
import itertools
import json
import logging
import optparse
import os
import re
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__ if __name__ != '__main__' else 'ymake_conf.py')


def init_logger(verbose):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)


class DebugString(object):
    def __init__(self, get_string_func):
        self.get_string_func = get_string_func

    def __str__(self):
        return self.get_string_func()


class ConfigureError(Exception):
    pass


class Platform(object):
    Linux = 'linux'
    MacOS = 'macos'
    Windows = 'windows'
    FreeBSD = 'freebsd'
    IOS = 'ios'
    Android = 'android'
    Cygwin = 'cygwin'

    X86_64 = 'x86_64'

    def __init__(self):
        self.name = None
        self.os = None
        self.arch = None

    @staticmethod
    def from_json(platform_json):
        """
        :rtype: Platform
        """
        self = Platform()
        self._os = platform_json['os']
        self._arch = platform_json['arch'].lower()
        self.name = platform_json.get('visible_name', platform_json['toolchain'])
        self.os = self._parse_os(self._os)
        self.arch = self._arch.lower()

        return self

    @staticmethod
    def create(name, os_, arch):
        """
        :rtype: Platform
        """
        self = Platform()
        self.name = name
        self.os = os_
        self.arch = arch
        return self

    @property
    def is_linux(self):
        return self.os == Platform.Linux

    @property
    def is_macos(self):
        return self.os == Platform.MacOS

    @property
    def is_windows(self):
        return self.os == Platform.Windows

    @property
    def is_freebsd(self):
        return self.os == Platform.FreeBSD

    @property
    def is_ios(self):
        return self.os == Platform.IOS

    @property
    def is_android(self):
        return self.os == Platform.Android

    @property
    def is_cygwin(self):
        return self.os == Platform.Cygwin

    @property
    def is_posix(self):
        return self.is_linux or self.is_macos or self.is_freebsd or self.is_ios or self.is_android or self.is_cygwin

    @property
    def is_32_bit(self):
        return self.arch in ('i386', 'i686', 'x86', 'arm')

    @property
    def is_64_bit(self):
        return self.arch == 'x86_64'

    @property
    def is_intel(self):
        return self.arch in ('i386', 'i686', 'x86', 'x86_64')

    @property
    def is_x86_64(self):
        return self.arch == 'x86_64'

    @property
    def is_arm(self):
        return self.arch == 'arm'

    @property
    def is_aarch64(self):
        return self.arch == 'aarch64'

    @property
    def os_variables(self):
        result = [
            self.os.upper(),  # 'LINUX' variable, for backward compatibility
            'OS_{name}'.format(name=self.os.upper()),  # 'OS_LINUX' variable
        ]
        if self.is_macos:
            result.extend(['DARWIN', 'OS_DARWIN'])
        return result

    @property
    def arch_variables(self):
        vs = []

        if self.is_32_bit:
            vs.append('ARCH_TYPE_32')
        if self.is_64_bit:
            vs.append('ARCH_TYPE_64')

        # Intel

        if self.arch in ('i386', 'i686'):
            vs.append('ARCH_I386')
        if self.arch == 'i686':
            vs.append('ARCH_I686')

        if self.arch in ('x86_64', 'amd64'):
            vs.append('ARCH_X86_64')

        # ARM

        if self.arch.startswith('arm'):
            vs.append('ARCH_ARM')
        if self.arch.startswith('arm7'):
            vs.append('ARCH_ARM7')
        if self.arch.startswith('arm64') or self.arch.startswith('armv8'):
            vs.append('ARCH_ARM64')
        if self.arch == 'aarch64':
            # TODO(somov): join arm64 and aarch64. Set ARCH_ARM for aarch64.
            vs.append('ARCH_AARCH64')

        # PowerPC

        if self.arch == 'ppc64le':
            vs.append('ARCH_PPC64LE')

        return vs

    def find_in_dict(self, dict_, default=None):
        if dict_ is None:
            return default
        for key in dict_.iterkeys():
            if self._parse_os(key) == self.os:
                return dict_[key]
        return default

    @property
    def os_compat(self):
        if self.os == Platform.MacOS:
            return 'DARWIN'
        else:
            return self.os.upper()

    def __str__(self):
        return '{name}-{os}-{arch}'.format(name=self.name, os=self.os, arch=self.arch)

    def __cmp__(self, other):
        return cmp((self.name, self.os, self.arch), (other.name, other.os, other.arch))

    def __hash__(self):
        return hash((self.name, self.os, self.arch))

    @staticmethod
    def _parse_os(os_):
        os_ = os_.lower()

        if os_ == 'linux':
            return Platform.Linux
        if os_ in ('darwin', 'macos'):
            return Platform.MacOS
        if os_ in ('windows', 'win', 'win32', 'win64'):
            return Platform.Windows
        if os_ == 'freebsd':
            return Platform.FreeBSD

        if os_ == 'ios':
            return Platform.IOS

        if os_ == 'android':
            return Platform.Android

        if os_.startswith('cygwin'):
            return Platform.Cygwin

        return os_.lower()


def which(prog):
    if os.path.exists(prog) and os.access(prog, os.X_OK):
        return prog

    # Ищем в $PATH только простые команды, без путей.
    if os.path.dirname(prog) != '':
        return None

    path = os.getenv('PATH', '')

    pathext = os.environ.get('PATHEXT')
    # На Windows %PATHEXT% указывает на список расширений, которые нужно проверять
    # при поиске команды в пути. Точное совпадение без расширения имеет приоритет.
    pathext = [''] if pathext is None else [''] + pathext.lower().split(os.pathsep)

    for dir_ in path.split(os.path.pathsep):
        for ext in pathext:
            p = os.path.join(dir_, prog + ext)
            if os.path.exists(p) and os.path.isfile(p) and os.access(p, os.X_OK):
                return p

    return None


def get_stdout_line(command, ignore_return_code=False):
    output = get_stdout(command, ignore_return_code)
    first_line, rest = output.split('\n', 1)
    if rest:
        logger.debug('Multiple lines in output from command %s: [\n%s\n]', command, output)
    return first_line


def get_stdout(command, ignore_return_code=False):
    stdout, code = get_stdout_and_code(command)
    return stdout if code == 0 or ignore_return_code else None


def get_stdout_and_code(command, env=None):
    # noinspection PyBroadException
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        stdout, _ = process.communicate()
        return stdout, process.returncode
    except Exception:
        return None, None


def to_strings(o):
    if isinstance(o, (list, tuple)):
        for s in o:
            for ss in to_strings(s):
                yield ss
    else:
        if o is not None:
            if isinstance(o, bool):
                yield 'yes' if o else 'no'
            elif isinstance(o, (str, int)):
                yield str(o)
            else:
                raise ConfigureError('Unexpected value {} {}'.format(type(o), o))


def emit(key, *value):
    print '{0}={1}'.format(key, ' '.join(to_strings(value)))


def append(key, *value):
    print '{0}+={1}'.format(key, ' '.join(to_strings(value)))


def emit_big(text):
    prefix = None
    first = True
    for line in text.split('\n'):
        if prefix is None:
            if not line:
                continue

            prefix = 0
            while prefix < len(line) and line[prefix] == ' ':
                prefix += 1

        if first:  # Be pretty, prepend an empty line before the output
            print
            first = False

        print line[prefix:]


class Variables(dict):
    def emit(self):
        for k in sorted(self.keys()):
            emit(k, self[k])

    def update_from_presets(self):
        for k in self.iterkeys():
            v = preset(k)
            if v is not None:
                self[k] = v

    def reset_if_any(self, value_check=None, reset_value=None):
        if value_check is None:
            def value_check(v_):
                return v_ is None

        if any(map(value_check, self.itervalues())):
            for k in self.iterkeys():
                self[k] = reset_value


def reformat_env(env, values_sep=':'):
    def format_var(name, values):
        return '${{env:"{}={}"}}'.format(name, ('\\' + values_sep).join(values))

    return ' '.join(format_var(name, values) for name, values in env.iteritems())


# TODO(somov): Проверить, используется ли это. Может быть, выпилить.
def userify_presets(presets, keys):
    for key in keys:
        user_key = 'USER_{}'.format(key)
        values = [presets.pop(key, None), presets.get(user_key)]
        presets[user_key] = ' '.join(filter(None, values))


def preset(key, default=None):
    return opts().presets.get(key, default)


def is_positive(key):
    return preset(key, '').lower() in ('yes', 'true', 'on')


def is_negative(key):
    return preset(key, '').lower() in ('no', 'false', 'off')


def select(selectors, default=None):
    for value, enabled in selectors:
        if enabled:
            return value
    return default


class Options(object):
    def __init__(self, argv):
        def parse_presets(raw_presets):
            presets = {}
            for p in raw_presets:
                toks = p.split('=', 1)
                name = toks[0]
                value = toks[1] if len(toks) >= 2 else ''
                presets[name] = value
            return presets

        parser = optparse.OptionParser(add_help_option=False)
        opt_group = optparse.OptionGroup(parser, 'Conf script options')
        opt_group.add_option('--toolchain-params', dest='toolchain_params', action='store', help='Set toolchain params via file')
        opt_group.add_option('-D', '--preset', dest='presets', action='append', default=[], help='set or override presets')
        opt_group.add_option('-l', '--local-distbuild', dest='local_distbuild', action='store_true', default=False, help='conf for local distbuild')
        parser.add_option_group(opt_group)

        self.options, self.arguments = parser.parse_args(argv)

        argv = self.arguments
        if len(argv) < 4:
            print >> sys.stderr, 'Usage: ArcRoot, --BuildType--, Verbosity, [Path to local.ymake]'
            sys.exit(1)

        self.arcadia_root = argv[1]
        init_logger(argv[3] == 'verbose')

        # Эти переменные не надо использоваться напрямую. Их значения уже разбираются в других местах.
        self.build_type = argv[2].lower()
        self.local_distbuild = self.options.local_distbuild
        self.toolchain_params = self.options.toolchain_params

        self.presets = parse_presets(self.options.presets)
        userify_presets(self.presets, ('CFLAGS', 'CXXFLAGS', 'CONLYFLAGS'))

    Instance = None


def opts():
    if Options.Instance is None:
        Options.Instance = Options(sys.argv)
    return Options.Instance


class Profiler(object):
    Generic = 'generic'
    GProf = 'gprof'


class Arcadia(object):
    def __init__(self, root):
        self.root = root


class Build(object):
    def __init__(self, arcadia, build_type, toolchain_params, force_ignore_local_files=False):
        self.arcadia = arcadia
        self.params = self._load_json_from_base64(toolchain_params)
        self.build_type = build_type

        platform = self.params['platform']
        self.host = Platform.from_json(platform['host'])
        self.target = Platform.from_json(platform['target'])

        self.tc = self._get_toolchain_options()

        # TODO(somov): Удалить, когда перестанет использоваться.
        self.build_system = 'ymake'

        self.ignore_local_files = False

        dist_prefix = 'dist-'
        if self.build_type.startswith(dist_prefix):
            self.build_system = 'distbuild'
            self.build_type = self.build_type[len(dist_prefix):]

        if force_ignore_local_files:
            self.ignore_local_files = True

        if self.is_ide_build_type(self.build_type):
            self.ignore_local_files = True

        self.pic = not is_positive('FORCE_NO_PIC')

    def print_build(self):
        self._print_build_settings()

        host_os = System(self.host)
        host_os.print_host_settings()

        target_os = System(self.target)
        target_os.print_target_settings()

        if self.pic:
            emit('PIC', 'yes')

        emit('COMPILER_ID', self.tc.type.upper())

        if self.is_valgrind:
            emit('WITH_VALGRIND', 'yes')

        toolchain_type, compiler_type, linker_type = Compilers[self.tc.type]
        toolchain = toolchain_type(self.tc, self)
        compiler = compiler_type(self.tc, self)
        linker = linker_type(self.tc, self)

        toolchain.print_toolchain()
        compiler.print_compiler()
        linker.print_linker()

        self._print_other_settings(compiler)

    def _print_build_settings(self):
        emit('BUILD_TYPE', self.build_type.upper())
        emit('BT_' + self.build_type.upper().replace('-', '_'), 'yes')

        if self.build_system == 'distbuild':
            emit('DISTBUILD', 'yes')

            if is_positive('NO_YMAKE'):
                ymake_python = '$(PYTHON)/python'
            else:
                ymake_python = '{} --python'.format(preset('MY_YMAKE_BIN') or '$(YMAKE)/ymake')
        elif self.build_system == 'ymake':
            ymake_python = '$YMAKE_BIN --python'
        else:
            raise ConfigureError()

        emit('YMAKE_PYTHON', ymake_python)
        emit('YMAKE_UNPICKLER', ymake_python, '$ARCADIA_ROOT/build/plugins/_unpickler.py')

    @property
    def is_release(self):
        # TODO(somov): Проверить, бывают ли тут суффиксы на самом деле
        return self.build_type in ('release', 'relwithdebinfo', 'profile', 'gprof') or self.build_type.endswith('-release')

    @property
    def is_debug(self):
        return self.build_type == 'debug' or self.build_type.endswith('-debug')

    @property
    def is_coverage(self):
        return self.build_type == 'coverage'

    @property
    def is_sanitized(self):
        return preset('SANITIZER_TYPE')

    @property
    def with_ndebug(self):
        return self.build_type in ('release', 'valgrind-release', 'profile', 'gprof')

    @property
    def is_valgrind(self):
        return self.build_type == 'valgrind' or self.build_type == 'valgrind-release'

    @property
    def is_ide(self):
        return self.is_ide_build_type(self.build_type)

    @property
    def profiler_type(self):
        if self.build_type == 'profile':
            return Profiler.Generic
        elif self.build_type == 'gprof':
            return Profiler.GProf
        else:
            return None

    @staticmethod
    def is_ide_build_type(build_type):
        return build_type == 'nobuild'

    def _get_toolchain_options(self):
        type_ = self.params['params']['type']

        if type_ == 'system_cxx':
            detector = CompilerDetector()
            detector.detect(self.params['params'].get('c_compiler'), self.params['params'].get('cxx_compiler'))
            type_ = detector.type
        else:
            detector = None

        if type_ == 'msvc':
            return MSVCToolchainOptions(self, detector)
        else:
            return GnuToolchainOptions(self, detector)

    def _print_other_settings(self, compiler):
        host = self.host

        emit('USE_LOCAL_TOOLS', 'no' if self.ignore_local_files else 'yes')

        ragel = Ragel()
        ragel.configure_toolchain(self, compiler)
        ragel.print_variables()

        perl = Perl()
        perl.configure_local()
        perl.print_variables('LOCAL_')

        yasm = Yasm(self.target)
        yasm.configure()
        yasm.print_variables()

        if host.is_linux or host.is_freebsd or host.is_macos or host.is_cygwin:
            if is_negative('USE_ARCADIA_PYTHON'):
                python = Python(self.tc)
                python.configure_posix()
                python.print_variables()

        cuda = Cuda(self)
        cuda.print_variables()

        if self.ignore_local_files or host.is_windows or is_positive('NO_SVN_DEPENDS'):
            emit('SVN_DEPENDS')
        else:
            def find_svn():
                for i in range(0, 3):
                    for path in (['.svn', 'wc.db'], ['.svn', 'entries'], ['.git', 'HEAD']):
                        path_parts = [self.arcadia.root] + [os.pardir] * i + path
                        full_path = os.path.join(*path_parts)
                        # HACK(somov): No "normpath" here. ymake fails with the "source file name is outside the build tree" error
                        # when .svn/wc.db found in "trunk" instead of "arcadia". But $ARCADIA_ROOT/../.svn/wc.db is ok.
                        if os.path.exists(full_path):
                            return '${input;hide:"%s"}' % full_path
                return ''

            emit('SVN_DEPENDS', find_svn())

    @staticmethod
    def _load_json_from_base64(base64str):
        """
        :rtype: dict[str, Any]
        """

        def un_unicode(o):
            if isinstance(o, unicode):
                return o.encode('utf-8')
            if isinstance(o, list):
                return [un_unicode(oo) for oo in o]
            if isinstance(o, dict):
                return {un_unicode(k): un_unicode(v) for k, v in o.iteritems()}
            return o

        return un_unicode(json.loads(base64.b64decode(base64str)))


class YMake(object):
    def __init__(self, arcadia):
        self.arcadia = arcadia

    @staticmethod
    def print_presets():
        if opts().presets:
            print '# Variables set from command line by -D options'
            for key in opts().presets:
                print '{0}={1}'.format(key, opts().presets[key])

    def print_core_conf(self):
        with open(self._find_core_conf(), 'r') as fin:
            print fin.read()

    def print_settings(self):
        emit('ARCADIA_ROOT', self.arcadia.root)

    @staticmethod
    def _find_core_conf():
        script_dir = os.path.dirname(__file__)
        full_path = os.path.join(script_dir, 'ymake.core.conf')
        if os.path.exists(full_path):
            return full_path
        return None


class System(object):
    def __init__(self, platform):
        self.platform = platform

    def print_windows_target_const(self):
        # TODO(somov): Remove this variables, use generic OS/arch variables in makelists.
        emit('WINDOWS', 'yes')
        emit('WIN32', 'yes')
        if self.platform.is_64_bit == 64:
            emit('WIN64', 'yes')

    def print_nix_target_const(self):
        emit('JAVA_INCLUDE', '-I{0}/include -I{0}/include/{1}'.format('/usr/lib/jvm/default-java', self.platform.os_compat))

        emit('UNIX', 'yes')
        emit('REALPRJNAME')
        emit('SONAME')

    @staticmethod
    def print_nix_host_const():
        emit('WRITE_COMMAND', '/bin/echo', '-e')

        print '''
when ($USE_PYTHON) {
    C_DEFINES+= -DUSE_PYTHON
}'''

    @staticmethod
    def print_freebsd_const():
        emit('FREEBSD_VER', '9')
        emit('FREEBSD_VER_MINOR', '0')

        print '''
when (($USEMPROF == "yes") || ($USE_MPROF == "yes")) {
    C_LIBRARY_PATH+=-L/usr/local/lib
    C_SYSTEM_LIBRARIES_INTERCEPT+=-lc_mp
}
when (($USEMPROF == "yes") || ($USE_MPROF == "yes")) {
    C_DEFINES+= -DUSE_MPROF
}
'''

    @staticmethod
    def print_linux_const():
        print '''
when (($USEMPROF == "yes") || ($USE_MPROF == "yes")) {
    C_SYSTEM_LIBRARIES_INTERCEPT+=-ldmalloc
}
'''

    def print_target_settings(self):
        emit('TARGET_PLATFORM', self.platform.os_compat)
        emit('HARDWARE_ARCH', '32' if self.platform.is_32_bit else '64')
        emit('HARDWARE_TYPE', self.platform.arch)

        for variable in self.platform.arch_variables:
            emit(variable, 'yes')

        for variable in self.platform.os_variables:
            emit(variable, 'yes')

        if self.platform.is_posix:
            self.print_nix_target_const()
            if self.platform.is_linux:
                self.print_linux_const()
            elif self.platform.is_freebsd:
                self.print_freebsd_const()
        elif self.platform.is_windows:
            self.print_windows_target_const()

        self.print_target_shortcuts()

    # Misc target arch-related shortcuts
    def print_target_shortcuts(self):
        if preset('HAVE_MKL') is None:
            print 'HAVE_MKL=no'
            if self.platform.is_linux:
                print '''
  when ($ARCH_X86_64 && !$SANITIZER_TYPE) {
      HAVE_MKL=yes
  }
'''

    def print_host_settings(self):
        emit('HOST_PLATFORM', self.platform.os_compat)
        if not self.platform.is_windows:
            self.print_nix_host_const()

        for variable in itertools.chain(self.platform.os_variables, self.platform.arch_variables):
            emit('HOST_{var}'.format(var=variable), 'yes')


class CompilerDetector(object):
    def __init__(self):
        self.type = None
        self.c_compiler = None
        self.cxx_compiler = None
        self.version_list = None

    @staticmethod
    def preprocess_source(compiler, source):
        # noinspection PyBroadException
        try:
            fd, path = tempfile.mkstemp(suffix='.cpp')
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(source)
                stdout, code = get_stdout_and_code([compiler, '-E', path])
            finally:
                os.remove(path)
            return stdout, code

        except Exception as e:
            logger.debug('Preprocessing failed: %s', e)
            return None, None

    @staticmethod
    def get_compiler_vars(compiler, names):
        prefix = '____YA_VAR_'
        source = '\n'.join(['{prefix}{name}={name}\n'.format(prefix=prefix, name=n) for n in names])

        # Некоторые препроцессоры возвращают ненулевой код возврата. Поэтому его проверять нельзя.
        # Мы можем только удостовериться после разбора stdout, что в нём
        # присутствовала хотя бы одна подставленная переменная.
        # TODO(somov): Исследовать, можно ли проверять ограниченный набор кодов возврата.
        stdout, _ = CompilerDetector.preprocess_source(compiler, source)

        if stdout is None:
            return None

        vars_ = {}
        for line in stdout.split('\n'):
            parts = line.split('=', 1)
            if len(parts) == 2 and parts[0].startswith(prefix):
                name, value = parts[0][len(prefix):], parts[1]
                if value == name:
                    continue  # Preprocessor variable was not substituted
                vars_[name] = value

        return vars_

    def detect(self, c_compiler=None, cxx_compiler=None):
        c_compiler = c_compiler or os.environ.get('CC')
        cxx_compiler = cxx_compiler or os.environ.get('CXX') or c_compiler
        c_compiler = c_compiler or cxx_compiler

        logger.debug('e=%s', os.environ)
        if c_compiler is None:
            raise ConfigureError('Custom compiler was requested but not specified')

        c_compiler_path = which(c_compiler)

        clang_vars = ['__clang_major__', '__clang_minor__', '__clang_patchlevel__']
        gcc_vars = ['__GNUC__', '__GNUC_MINOR__', '__GNUC_PATCHLEVEL__']
        msvc_vars = ['_MSC_VER']
        apple_var = '__apple_build_version__'

        compiler_vars = self.get_compiler_vars(c_compiler_path, clang_vars + [apple_var] + gcc_vars + msvc_vars)

        if not compiler_vars:
            raise ConfigureError('Could not determine custom compiler version: {}'.format(c_compiler))

        def version(version_names):
            def iter_version():
                for name in version_names:
                    yield int(compiler_vars[name])

            # noinspection PyBroadException
            try:
                return list(iter_version())
            except Exception:
                return None

        clang_version = version(clang_vars)
        apple_build = apple_var in compiler_vars
        # TODO(somov): Учитывать номера версий сборки Apple компилятора Clang.
        _ = apple_build
        gcc_version = version(gcc_vars)
        msvc_version = version(msvc_vars)

        if clang_version:
            logger.debug('Detected Clang version %s', clang_version)
            self.type = 'clang'
        elif gcc_version:
            logger.debug('Detected GCC version %s', gcc_version)
            # TODO(somov): Переименовать в gcc.
            self.type = 'gnu'
        elif msvc_version:
            logger.debug('Detected MSVC version %s', msvc_version)
            self.type = 'msvc'
        else:
            raise ConfigureError('Could not determine custom compiler type: {}'.format(c_compiler))

        self.version_list = clang_version or gcc_version or msvc_version

        self.c_compiler = c_compiler_path
        self.cxx_compiler = cxx_compiler and which(cxx_compiler) or c_compiler_path


class ToolchainOptions(object):
    def __init__(self, build, detector):
        """
        :type build: Build
        """
        self.host = build.host
        self.target = build.target

        tc_json = build.params

        logger.debug('Toolchain host %s', self.host)
        logger.debug('Toolchain target %s', self.target)
        logger.debug('Toolchain json %s', DebugString(lambda: json.dumps(tc_json, indent=4, sort_keys=True)))

        self.params = tc_json['params']
        self._name = tc_json.get('name', 'theyknow')

        if detector:
            self.type = detector.type
            self.from_arcadia = False

            self.c_compiler = detector.c_compiler
            self.cxx_compiler = detector.cxx_compiler
            self.compiler_version_list = detector.version_list
            self.compiler_version = '.'.join(map(str, self.compiler_version_list))

        else:
            self.type = self.params['type']
            self.from_arcadia = True

            self.c_compiler = self.params['c_compiler']
            self.cxx_compiler = self.params['cxx_compiler']

            # TODO(somov): Требовать номер версии всегда.
            self.compiler_version = self.params.get('gcc_version', '0')
            self.compiler_version_list = map(int, self.compiler_version.split('.'))

        # TODO(somov): Посмотреть, можно ли спрятать это поле.
        self.name_marker = '$(%s)' % self.params.get('match_root', self._name.upper())

        self.arch_opt = self.params.get('arch_opt', [])
        self.target_opt = self.params.get('target_opt', [])

        # TODO(somov): Убрать чтение настройки из os.environ.
        self.werror_mode = preset('WERROR_MODE') or os.environ.get('WERROR_MODE') or self.params.get('werror_mode') or 'compiler_specific'

        self._env = tc_json.get('env', {})

        logger.debug('c_compiler=%s', self.c_compiler)
        logger.debug('cxx_compiler=%s', self.cxx_compiler)

    def version_at_least(self, *args):
        return args <= tuple(self.compiler_version_list)

    @property
    def is_clang(self):
        return self.type == 'clang'

    @property
    def is_from_arcadia(self):
        return self.from_arcadia

    def get_env(self, convert_list=None):
        convert_list = convert_list or (lambda x: x)
        r = {}
        for k, v in self._env.iteritems():
            if isinstance(v, str):
                r[k] = v
            elif isinstance(v, list):
                r[k] = convert_list(v)
            else:
                logger.debug('Unexpected values in environment: %s', self._env)
                raise ConfigureError('Internal error')
        return r


class GnuToolchainOptions(ToolchainOptions):
    def __init__(self, build, detector):
        super(GnuToolchainOptions, self).__init__(build, detector)

        self.ar = self.params.get('ar') or 'ar'
        self.ar_plugin = self.params.get('ar_plugin')

        self.dwarf_tool = self.target.find_in_dict(self.params.get('dwarf_tool'))
        if self.dwarf_tool is None and self.host.os == Platform.MacOS:
            self.dwarf_tool = 'dsymutil -f'

        # TODO(somov): Унифицировать формат sys_lib
        self.sys_lib = self.params.get('sys_lib', {})
        if isinstance(self.sys_lib, dict):
            self.sys_lib = self.target.find_in_dict(self.sys_lib, [])

        self.compiler_platform_projects = self.target.find_in_dict(self.params.get('platform'), [])

        self.os_sdk = preset('OS_SDK') or self._default_os_sdk()
        self.os_sdk_local = self.os_sdk == 'local'

    def _default_os_sdk(self):
        if self.target.is_linux:
            if self.target.is_x86_64:
                if self.host.is_linux and not self.version_at_least(4, 0):
                    # Clang 3.9 works with local OS SDK by default.
                    # Next versions of Clang will use fixed OS SDK already.
                    return 'local'

            elif self.target.is_aarch64:
                # Earliest Ubuntu SDK available for AArch64
                return 'ubuntu-16'

            # Default OS SDK for Linux builds
            return 'ubuntu-12'


class MSVCToolchainOptions(ToolchainOptions):
    def __init__(self, build, detector):
        super(MSVCToolchainOptions, self).__init__(build, detector)

        # C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC
        self.vc_root = None

        # C:\Program Files (x86)\Windows Kits\10\Include\10.0.14393.0
        self.kit_includes = None

        # C:\Program Files (x86)\Windows Kits\10\Lib\10.0.14393.0
        self.kit_libs = None

        self.ide_msvs = 'ide_msvs' in self.params
        if self.ide_msvs:
            bindir = '$(VC_ExecutablePath_x64_x64)\\'
            self.c_compiler = bindir + 'cl.exe'
            self.cxx_compiler = self.c_compiler

            self.link = bindir + 'link.exe'
            self.lib = bindir + 'lib.exe'
            self.masm_compiler = bindir + 'ml64.exe'

            self.vc_root = '$(VCInstallDir)'
            # TODO(somov): Починить
            self.kit_includes = None
            self.kit_libs = None

        elif detector:
            self.masm_compiler = which('ml64.exe')
            self.link = which('link.exe')
            self.lib = which('lib.exe')

            sdk_dir = os.environ.get('WindowsSdkDir')
            sdk_version = os.environ.get('WindowsSDKVersion')
            vc_install_dir = os.environ.get('VCINSTALLDIR')

            if any([x is None for x in (sdk_dir, sdk_version, vc_install_dir)]):
                raise ConfigureError('No %WindowsSdkDir%, %WindowsSDKVersion% or %VCINSTALLDIR% present. Please, run vcvars64.bat to setup preferred environment.')

            self.vc_root = os.path.normpath(vc_install_dir)
            self.kit_includes = os.path.normpath(os.path.join(sdk_dir, 'Include', sdk_version))
            self.kit_libs = os.path.normpath(os.path.join(sdk_dir, 'Lib', sdk_version))

        else:
            sdk_root = self.params['sdk_root']
            self.vc_root = os.path.join(sdk_root, 'VC')
            self.kit_includes = os.path.join(sdk_root, 'include')
            self.kit_libs = os.path.join(sdk_root, 'lib')

            self.masm_compiler = self.params['masm_compiler']
            self.link = self.params['link']
            self.lib = self.params['lib']

        self.under_wine = 'wine' in self.params
        self.system_msvc = 'system_msvc' in self.params


class Toolchain(object):
    def __init__(self, tc, build):
        self.tc = tc
        self.build = build

    def print_toolchain(self):
        raise NotImplementedError()


class Compiler(object):
    def __init__(self, tc, compiler_variable):
        self.compiler_variable = compiler_variable
        self.tc = tc

    def print_compiler(self):
        # CLANG and CLANG_VER variables
        emit(self.compiler_variable, 'yes')
        emit('{}_VER'.format(self.compiler_variable), self.tc.compiler_version)


class GnuToolchain(Toolchain):
    def __init__(self, tc, build):
        super(GnuToolchain, self).__init__(tc, build)
        self.c_flags_platform = list(tc.target_opt)

        self.c_flags_platform.extend(select(default=[], selectors=[
            (['-mmacosx-version-min=10.9'], build.target.is_macos),
            (['-mios-version-min=7.0'], build.target.is_ios),
        ]))

    def print_toolchain(self):
        emit('TOOLCHAIN_ENV', reformat_env(self.tc.get_env(), values_sep=':'))
        emit('C_FLAGS_PLATFORM', self.c_flags_platform)

        if preset('OS_SDK') is None:
            emit('OS_SDK', self.tc.os_sdk)
            emit('PERL_OS_SDK', 'ubuntu-12')
        else:
            # temporary https://st.yandex-team.ru/DEVTOOLS-4027
            emit('PERL_OS_SDK', self.tc.os_sdk)
        emit('OS_SDK_ROOT', None if self.tc.os_sdk_local else '$(OS_SDK_ROOT)')


class GnuCompiler(Compiler):
    gcc_fstack = ['-fstack-protector']

    def __init__(self, tc, build, compiler_variable):
        """
        :type tc: GnuToolchainOptions
        :type build: Build
        """
        super(GnuCompiler, self).__init__(tc, compiler_variable)

        self.build = build
        self.host = self.build.host
        self.target = self.build.target
        self.tc = tc

        self.c_defines = ['-D_FILE_OFFSET_BITS=64', '-D_LARGEFILE_SOURCE',
                          '-D__STDC_CONSTANT_MACROS', '-D__STDC_FORMAT_MACROS', '-DGNU']

        if self.target.is_linux or self.target.is_cygwin:
            self.c_defines.append('-D_GNU_SOURCE')

        self.extra_compile_opts = []

        self.c_flags = self.tc.arch_opt + ['-pipe']
        self.c_only_flags = []
        self.cxx_flags = []

        self.sfdl_flags = ['-E', '-C', '-x', 'c++']

        if self.target.is_intel:
            if self.target.is_32_bit:
                self.c_flags.append('-m32')
            if self.target.is_64_bit:
                self.c_flags.append('-m64')

        enable_sse = self.target.is_intel
        if self.target.is_ios:
            # TODO(somov): Расследовать.
            # contrib/libs/crcutil не собирается под clang37-ios-i386 со включенным SSE.
            # multiword_64_64_gcc_i386_mmx.cc:98:5: error: inline assembly requires more registers than available
            enable_sse = False

        if enable_sse:
            # TODO(somov): Удалить define-ы и сборочный флаг
            gcc_sse_opts = {'-msse': '-DSSE_ENABLED=1', '-msse2': '-DSSE2_ENABLED=1', '-msse3': '-DSSE3_ENABLED=1'}
            if not is_positive('NOSSE'):
                for opt, define in gcc_sse_opts.iteritems():
                    self.c_flags.append(opt)
                    self.c_defines.append(define)
            else:
                self.c_defines.append('-no-sse')

        self.cross_suffix = '' if is_positive('FORCE_NO_PIC') else '.pic'

        self.optimize = None

        self.configure_build_type()

    def configure_build_type(self):
        if self.build.is_valgrind:
            self.c_defines.append('-DWITH_VALGRIND=1')

        if self.build.is_debug:
            self.c_flags.append('$FSTACK')

        if self.build.is_release:
            self.c_flags.append('$OPTIMIZE')
            self.optimize = '-O2'

        if self.build.with_ndebug:
            self.c_defines.append('-DNDEBUG')
        else:
            self.c_defines.append('-UNDEBUG')

        if self.build.is_coverage:
            self.c_flags.extend(['-fprofile-arcs', '-ftest-coverage'])
        if self.build.profiler_type in (Profiler.Generic, Profiler.GProf):
            self.c_flags.append('-fno-omit-frame-pointer')

        if self.build.profiler_type == Profiler.GProf:
            self.c_flags.append('-pg')

    def print_compiler(self):
        super(GnuCompiler, self).print_compiler()

        emit('C_COMPILER_UNQUOTED', self.tc.c_compiler)
        emit('C_COMPILER', '${quo:C_COMPILER_UNQUOTED}')
        emit('OPTIMIZE', self.optimize)
        emit('WERROR_MODE', self.tc.werror_mode)
        emit('FSTACK', self.gcc_fstack)
        append('C_DEFINES', self.c_defines, '-D_THREAD_SAFE', '-D_PTHREADS', '-D_REENTRANT')
        emit('DUMP_DEPS')
        emit('GCC_PREPROCESSOR_OPTS', '$DUMP_DEPS', '$C_DEFINES')
        append('C_WARNING_OPTS', '-Wall', '-W', '-Wno-parentheses')
        append('CXX_WARNING_OPTS', '-Woverloaded-virtual')
        append('USER_CFLAGS_GLOBAL', '')
        append('USER_CFLAGS_GLOBAL', '')

        emit_big('''
            when ($PIC && $PIC == "yes") {
                PICFLAGS=-fPIC
            }
            otherwise {
                PICFLAGS=
            }''')

        append('CFLAGS', self.c_flags, '$DEBUG_INFO_FLAGS', '$GCC_PREPROCESSOR_OPTS', '$C_WARNING_OPTS', '$PICFLAGS', '$USER_CFLAGS', '$USER_CFLAGS_GLOBAL',
               '-DFAKEID=$FAKEID', '-DARCADIA_ROOT=${ARCADIA_ROOT}', '-DARCADIA_BUILD_ROOT=${ARCADIA_BUILD_ROOT}')
        append('CXXFLAGS', '$CXX_WARNING_OPTS', '-std=c++14', '$CFLAGS', self.cxx_flags, '$USER_CXXFLAGS')
        append('CONLYFLAGS', self.c_only_flags, '$USER_CONLYFLAGS')
        emit('CXX_COMPILER_UNQUOTED', self.tc.cxx_compiler)
        emit('CXX_COMPILER', '${quo:CXX_COMPILER_UNQUOTED}')
        emit('NOGCCSTACKCHECK', 'yes')
        emit('USE_GCCFILTER', preset('USE_GCCFILTER') or 'yes')
        emit('USE_GCCFILTER_COLOR', preset('USE_GCCFILTER_COLOR') or 'yes')
        emit('SFDL_FLAG', self.sfdl_flags, '-o', '$SFDL_TMP_OUT')
        emit('WERROR_FLAG', '-Werror', '-Wno-error=deprecated-declarations')
        # TODO(somov): Убрать чтение настройки из os.environ
        emit('USE_ARC_PROFILE', 'yes' if preset('USE_ARC_PROFILE') or os.environ.get('USE_ARC_PROFILE') else 'no')
        emit('DEBUG_INFO_FLAGS', '-g')

        platform_projects = self.tc.compiler_platform_projects
        if platform_projects:
            emit('COMPILER_PLATFORM', platform_projects)

        emit_big('''
            when ($NO_COMPILER_WARNINGS == "yes") {
                CFLAGS+= -w
            }
            when ($NO_OPTIMIZE == "yes") {
                OPTIMIZE=-O0
            }
            when ($SAVE_TEMPS ==  "yes") {
                CXXFLAGS += -save-temps
            }
            when ($NOGCCSTACKCHECK != "yes") {
                FSTACK+= -fstack-check
            }
            when ($NO_WSHADOW == "yes") {
                CFLAGS += -Wno-shadow
            }
            macro MSVC_FLAGS(Flags...) {
                # TODO: FIXME
                ENABLE(UNUSED_MACRO)
            }''')

        append('C_WARNING_OPTS', '-Wno-deprecated')
        append('CXX_WARNING_OPTS', '-Wno-invalid-offsetof')
        append('CXX_WARNING_OPTS', '-Wno-attributes')

        if self.tc.is_clang and self.tc.version_at_least(3, 9):
            append('CXX_WARNING_OPTS', '-Wno-undefined-var-template')

        # TODO(somov): Check whether this specific architecture is needed.
        if self.target.arch == 'i386':
            append('CFLAGS', '-march=pentiumpro')
            append('CFLAGS', '-mtune=pentiumpro')

        append('C_DEFINES', '-D__LONG_LONG_SUPPORTED')

        emit('GCC_COMPILE_FLAGS', '$EXTRA_C_FLAGS -c -o ${output:SRC%s.o}' % self.cross_suffix, '${input:SRC} ${pre=-I:INCLUDE}')
        emit('EXTRA_C_FLAGS')
        emit('EXTRA_COVERAGE_OUTPUT', '${output;noauto;hide:SRC%s.gcno}' % self.cross_suffix)
        emit('YNDEXER_OUTPUT_FILE', '${output;noauto:SRC%s.ydx.pb2}' % self.cross_suffix)  # should be the last output

        if is_positive('DUMP_COMPILER_DEPS'):
            emit('DUMP_DEPS', '-MD', '${output;hide;noauto:SRC.o.d}')
        elif is_positive('DUMP_COMPILER_DEPS_FAST'):
            emit('DUMP_DEPS', '-E', '-M', '-MF', '${output;noauto:SRC.o.d}')

        if not self.build.is_coverage:
            emit('EXTRA_OUTPUT')
        else:
            emit('EXTRA_OUTPUT', '${output;noauto;hide:SRC%s.gcno}' % self.cross_suffix)

        append('EXTRA_OUTPUT')

        style = ['${hide;kv:"p CC"} ${hide;kv:"pc green"}']
        cxx_args = ['$GCCFILTER', '$YNDEXER_ARGS', '$CXX_COMPILER', '$C_FLAGS_PLATFORM', '$GCC_COMPILE_FLAGS', '$CXXFLAGS', '$EXTRA_OUTPUT', '$SRCFLAGS', '$TOOLCHAIN_ENV', '$YNDEXER_OUTPUT'] + style
        c_args = ['$GCCFILTER', '$YNDEXER_ARGS', '$C_COMPILER', '$C_FLAGS_PLATFORM', '$GCC_COMPILE_FLAGS', '$CFLAGS', '$CONLYFLAGS', '$EXTRA_OUTPUT', '$SRCFLAGS', '$TOOLCHAIN_ENV', '$YNDEXER_OUTPUT'] + style

        print 'macro _SRC_cpp(SRC, SRCFLAGS...) {\n .CMD=%s\n}' % ' '.join(cxx_args)
        print 'macro _SRC_c(SRC, SRCFLAGS...) {\n .CMD=%s\n}' % ' '.join(c_args)
        print 'macro _SRC_m(SRC, SRCFLAGS...) {\n .CMD=$SRC_c($SRC $SRCFLAGS)\n}'
        print 'macro _SRC_masm(SRC, SRCFLAGS...) {\n}'


class GCC(GnuCompiler):
    def __init__(self, tc, build):
        super(GCC, self).__init__(tc, build, 'GCC')

        if self.tc.version_at_least(4, 9):
            self.c_flags.append('-fno-delete-null-pointer-checks')
            self.c_flags.append('-fabi-version=8')


class Clang(GnuCompiler):
    def __init__(self, tc, build):
        super(Clang, self).__init__(tc, build, 'CLANG')

        self.sfdl_flags.append('-Qunused-arguments')

        if self.tc.version_at_least(3, 6):
            self.c_flags.append('-Wno-inconsistent-missing-override')

        if self.tc.version_at_least(5, 0):
            self.c_flags.append('-Wno-c++17-extensions')
            self.c_flags.append('-Wno-exceptions')

    def print_compiler(self):
        super(Clang, self).print_compiler()

        # fuzzing configuration
        if self.tc.version_at_least(5,0):
            emit('FSANITIZE_FUZZER_SUPPORTED', 'yes')
            emit('LIBFUZZER_PATH', 'contrib/libs/libfuzzer-5.0')


class Linker(object):
    def __init__(self, tc, build):
        """
        :type tc: ToolchainOptions
        :type build: Build
        """
        self.tc = tc
        self.build = build

    def print_linker(self):
        self._print_linker_selector()

    def _print_linker_selector(self):
        if self.tc.is_clang and self.tc.version_at_least(3, 9) and self.build.host.is_linux and self.tc.is_from_arcadia:
            default_linker = 'lld'
            if is_positive('USE_LTO'):
                default_linker = 'gold'

            emit_big('''
                macro USE_LINKER() {
                    DEFAULT(_LINKER_ID %(default_linker)s)

                    when ($NOPLATFORM != "yes") {
                        when ($_LINKER_ID == "bfd") {
                            PEERDIR+=contrib/libs/platform/tools/linkers/bfd
                        }
                        when ($_LINKER_ID == "gold") {
                            PEERDIR+=contrib/libs/platform/tools/linkers/gold
                        }
                        when ($_LINKER_ID == "lld") {
                            PEERDIR+=contrib/libs/platform/tools/linkers/lld
                        }
                    }
                }''' % {'default_linker': default_linker})

        else:
            emit_big('''
                macro USE_LINKER() {
                    ENABLE(UNUSED_MACRO)
                }''')

        emit_big('''
            macro USE_LINKER_BFD() {
                SET(_LINKER_ID bfd)
            }
            macro USE_LINKER_GOLD() {
                SET(_LINKER_ID gold)
            }
            macro USE_LINKER_LLD() {
                SET(_LINKER_ID lld)
            }''')


class LD(Linker):
    def __init__(self, tc, build):
        """
        :type tc: GnuToolchainOptions
        :type build: Build
        """
        super(LD, self).__init__(tc, build)

        self.build = build
        self.host = self.build.host
        self.target = self.build.target
        self.tc = tc

        target = self.target

        self.ar = preset('AR') or self.tc.ar

        self.ld_flags = filter(None, [preset('LDFLAGS')])

        if target.is_linux:
            self.ld_flags.extend(['-ldl', '-lrt', '-Wl,--no-as-needed'])
        if target.is_android:
            self.ld_flags.extend(['-ldl', '-lsupc++', '-Wl,--no-as-needed'])
        if target.is_macos and not self.tc.is_clang:
            self.ld_flags.append('-Wl,-no_compact_unwind')

        self.link_pie_executables = target.is_android

        self.thread_library = select([
            ('-lpthread', target.is_linux or target.is_macos),
            ('-lthr', target.is_freebsd)
        ])

        self.rdynamic = None
        self.start_group = None
        self.end_group = None
        self.ld_stripflag = None
        self.use_stdlib = None
        self.soname_option = None
        self.dwarf_command = None
        self.libresolv = '-lresolv' if target.is_linux or target.is_macos or target.is_android else None

        if target.is_linux or target.is_android or target.is_freebsd:
            self.rdynamic = '-rdynamic'
            self.use_stdlib = '-nodefaultlibs'

        if target.is_linux or target.is_android or target.is_freebsd or target.is_cygwin:
            self.start_group = '-Wl,--start-group'
            self.end_group = '-Wl,--end-group'
            self.ld_stripflag = '-s'
            self.soname_option = '-soname'

        if target.is_macos or target.is_ios:
            self.use_stdlib = '-nodefaultlibs'
            self.soname_option = '-install_name'
            if not preset('NO_DEBUGINFO'):
                self.dwarf_command = '$DWARF_TOOL $TARGET -o ${output;pre=$REALPRJNAME.dSYM/Contents/Resources/DWARF/:REALPRJNAME}'

        if self.build.profiler_type == Profiler.GProf:
            self.ld_flags.append('-pg')

        if self.build.is_coverage:
            self.ld_flags.extend(('-fprofile-arcs', '-ftest-coverage'))

        # TODO(somov): Единое условие на coverage.
        if self.build.is_coverage or is_positive('GCOV_COVERAGE') or is_positive('CLANG_COVERAGE') or self.build.is_sanitized:
            self.use_stdlib = None

        # TODO(somov): Что-нибудь починить.
        # llvm-ar генерирует статические библиотеки, в которых объектные файлы иногда выровнены по 2 байта.
        # ld64 явно требует выравнивания по 4. В качестве костыля принудительно используем системный libtool.
        if target.is_macos and 'libtool' not in self.ar:
            self.ar = 'libtool'

    def print_linker(self):
        super(LD, self).print_linker()

        emit('AR_TOOL', self.ar)
        emit('AR_TYPE', 'AR' if 'libtool' not in self.ar else 'LIBTOOL')

        append('LDFLAGS', self.ld_flags)
        append('LDFLAGS_GLOBAL', '')

        emit('LD_STRIP_FLAG', self.ld_stripflag)
        emit('STRIP_FLAG')

        emit('C_LIBRARY_PATH')
        emit('C_SYSTEM_LIBRARIES_INTERCEPT')
        emit('C_SYSTEM_LIBRARIES', self.use_stdlib, self.thread_library, self.tc.sys_lib, '-lc')

        emit('DWARF_TOOL', self.tc.dwarf_tool)

        emit('OBJADDE')

        emit_big('''
            EXPORTS_VALUE=
            when ($EXPORTS_FILE) {
                EXPORTS_VALUE=-Wl,--version-script=${input:EXPORTS_FILE}
            }''')

        exe_flags = [
            '$C_FLAGS_PLATFORM', '${rootrel:SRCS_GLOBAL}', self.start_group, '${rootrel:PEERS}', self.end_group,
            '$EXPORTS_VALUE $LDFLAGS $LDFLAGS_GLOBAL $OBJADDE $OBJADDE_LIB',
            '$C_LIBRARY_PATH $C_SYSTEM_LIBRARIES_INTERCEPT $C_SYSTEM_LIBRARIES $STRIP_FLAG']

        pie_flag = '-pie' if self.link_pie_executables else None
        arch_flag = '--arch={arch}'.format(arch=self.target.os_compat)
        shared_flag = '-shared -Wl,{option},$SONAME'.format(option=self.soname_option)

        ld_env_style = '${cwd:ARCADIA_BUILD_ROOT} $TOOLCHAIN_ENV ${kv;hide:"p LD"} ${kv;hide:"pc light-blue"} ${kv;hide:"show_out"}'

        emit("GENERATE_MF",
             '$YMAKE_PYTHON', '${input:"build/scripts/generate_mf.py"}',
             '--build-root $ARCADIA_BUILD_ROOT --module-name $REALPRJNAME -o ${output;rootrel;pre=$MODULE_PREFIX;suf=$MODULE_SUFFIX.mf:REALPRJNAME}',
             '-t $MODULE_TYPE $NO_GPL_FLAG -Ya,lics $LICENSE_NAMES -Ya,peers ${rootrel:PEERS}',
             '${kv;hide:"p MF"} ${kv;hide:"pc light-green"}'
             )

        # Program

        emit('REAL_LINK_EXE',
             '$YMAKE_PYTHON ${input:"build/scripts/link_exe.py"}',
             '$GCCFILTER',
             '$CXX_COMPILER $AUTO_INPUT -o $TARGET', self.rdynamic, pie_flag, exe_flags,
             ld_env_style)

        # Shared Library

        emit('LINK_DYN_LIB_FLAGS')
        emit('REAL_LINK_DYN_LIB',
             '$YMAKE_PYTHON ${input:"build/scripts/link_dyn_lib.py"} --target $TARGET', arch_flag, '$LINK_DYN_LIB_FLAGS',
             '$CXX_COMPILER $AUTO_INPUT -o $TARGET', shared_flag, exe_flags,
             ld_env_style)

        if self.dwarf_command is None:
            emit('DWARF_COMMAND')
        else:
            emit('DWARF_COMMAND', self.dwarf_command, ld_env_style)
        emit('LINK_EXE', '$GENERATE_MF && $REAL_LINK_EXE && $DWARF_COMMAND')
        emit('LINK_DYN_LIB', '$GENERATE_MF && $REAL_LINK_DYN_LIB && $DWARF_COMMAND')
        emit('SWIG_DLL_JAR_CMD', '$GENERATE_MF && $REAL_SWIG_DLL_JAR_CMD && $DWARF_COMMAND')

        archiver = '$YMAKE_PYTHON ${input:"build/scripts/link_lib.py"} ${quo:AR_TOOL} $AR_TYPE $ARCADIA_BUILD_ROOT %s' % (self.tc.ar_plugin or 'None')

        # Static Library

        emit('LINK_LIB', '$GENERATE_MF &&', archiver, '$TARGET $AUTO_INPUT ${kv;hide:"p AR"}',
             '$TOOLCHAIN_ENV ${kv;hide:"pc light-red"} ${kv;hide:"show_out"}')

        # "Fat Object" : pre-linked global objects and static library with all dependencies

        # TODO(somov): Проверить, не нужны ли здесь все остальные флаги компоновки (LDFLAGS и т. д.).
        emit('LINK_FAT_OBJECT', '$GENERATE_MF &&',
             '$YMAKE_PYTHON ${input:"build/scripts/link_fat_obj.py"} --obj=$TARGET --lib=${output:REALPRJNAME.a}', arch_flag,
             '-Ya,input $AUTO_INPUT -Ya,global_srcs $SRCS_GLOBAL -Ya,peers $PEERS',
             '-Ya,linker $CXX_COMPILER $C_FLAGS_PLATFORM -Ya,archiver', archiver,
             '$TOOLCHAIN_ENV ${kv;hide:"p LD"} ${kv;hide:"pc light-blue"} ${kv;hide:"show_out"}')

        emit('LIBRT', '-lrt')
        emit('MD5LIB', '-lcrypt')
        emit('LIBRESOLV', self.libresolv)
        emit('PROFFLAG', '-pg')


class MSVC(object):
    # noinspection PyPep8Naming
    class WIN32_WINNT(object):
        Macro = '_WIN32_WINNT'
        Windows7 = '0x0601'
        Windows8 = '0x0602'

    def __init__(self, tc, build):
        """
        :type tc: MSVCToolchainOptions
        :type build: Build
        """
        if not isinstance(tc, MSVCToolchainOptions):
            raise TypeError('Got {} ({}) instead of an MSVCToolchainOptions'.format(tc, type(tc)))

        self.build = build
        self.tc = tc


class MSVCToolchain(Toolchain, MSVC):
    def __init__(self, tc, build):
        Toolchain.__init__(self, tc, build)
        MSVC.__init__(self, tc, build)

    def print_toolchain(self):
        emit('TOOLCHAIN_ENV', reformat_env(self.tc.get_env(), values_sep=';'))

        # TODO(somov): Заглушка для тех мест, где C_FLAGS_PLATFORM используется
        # для любых платформ. Нужно унифицировать с GnuToolchain.
        emit('C_FLAGS_PLATFORM')


class MSVCCompiler(Compiler, MSVC):
    def __init__(self, tc, build):
        Compiler.__init__(self, tc, 'MSVC')
        MSVC.__init__(self, tc, build)

    def print_compiler(self):
        super(MSVCCompiler, self).print_compiler()

        target = self.build.target

        win32_winnt = self.WIN32_WINNT.Windows7

        warns_enabled = [
            4018,  # 'expression' : signed/unsigned mismatch
            4265,  # 'class' : class has virtual functions, but destructor is not virtual
            4296,  # 'operator' : expression is always false
            4431,  # missing type specifier - int assumed
        ]
        warns_as_error = [
            4013,  # 'function' undefined; assuming extern returning int
        ]
        warns_disabled = [
            4127,  # conditional expression is constant
            4200,  # nonstandard extension used : zero-sized array in struct/union
            4201,  # nonstandard extension used : nameless struct/union
            4351,  # elements of array will be default initialized
            4355,  # 'this' : used in base member initializer list
            4503,  # decorated name length exceeded, name was truncated
            4510,  # default constructor could not be generated
            4511,  # copy constructor could not be generated
            4512,  # assignment operator could not be generated
            4554,  # check operator precedence for possible error; use parentheses to clarify precedence
            4610,  # 'object' can never be instantiated - user defined constructor required
            4706,  # assignment within conditional expression
            4800,  # forcing value to bool 'true' or 'false' (performance warning)
            4996,  # The POSIX name for this item is deprecated
            4714,  # function marked as __forceinline not inlined
            4197,  # 'TAtomic' : top-level volatile in cast is ignored
            4245,  # 'initializing' : conversion from 'int' to 'ui32', signed/unsigned mismatch
            4324,  # 'ystd::function<void (uint8_t *)>': structure was padded due to alignment specifier
        ]

        defines = [
            'WIN32',
            '_WIN32',
            '_WINDOWS',
            '_CRT_SECURE_NO_WARNINGS',
            '_CRT_NONSTDC_NO_WARNINGS',
            '_USE_MATH_DEFINES',
            '__STDC_CONSTANT_MACROS',
            '__STDC_FORMAT_MACROS',
            '_USING_V110_SDK71_',
            'SSE_ENABLED=1',
            'SSE2_ENABLED=1',
            'SSE3_ENABLED=1'
        ]

        winapi_unicode = False

        defines_debug = ['_DEBUG']
        defines_release = ['NDEBUG']

        print '''\
MSVC_INLINE_OPTIMIZED=yes
when ($MSVC_INLINE_OPTIMIZED == "yes") {
    MSVC_INLINE_FLAG=/Zc:inline
}
when ($MSVC_INLINE_OPTIMIZED == "no") {
    MSVC_INLINE_FLAG=/Zc:inline-
}
'''

        flags = ['/nologo', '/Zm500', '/GR', '/bigobj', '/FC', '/EHsc', '/errorReport:prompt', '$MSVC_INLINE_FLAG', '/DFAKEID=$FAKEID']
        flags += ['/we{}'.format(code) for code in warns_as_error]
        flags += ['/w1{}'.format(code) for code in warns_enabled]
        flags += ['/wd{}'.format(code) for code in warns_disabled]
        flags += self.tc.arch_opt

        flags_debug = ['/Ob0', '/Od'] + self._gen_defines(defines_debug)
        flags_release = ['/Ox', '/Ob2', '/Oi'] + self._gen_defines(defines_release)

        flags_cxx = []
        flags_c_only = []

        if target.is_arm:
            masm_io = '-o ${output:SRC.obj} ${input;msvs_source:SRC}'
        else:
            masm_io = '/nologo /c /Fo${output:SRC.obj} ${input;msvs_source:SRC}'

        if is_positive('USE_UWP'):
            flags_cxx += ['/ZW', '/AI{vc_root}/lib/store/references'.format(vc_root=self.tc.vc_root)]
            if self.tc.kit_includes:
                flags.append('/I{kit_includes}/winrt'.format(kit_includes=self.tc.kit_includes))
            win32_winnt = self.WIN32_WINNT.Windows8
            defines.append('WINAPI_FAMILY=WINAPI_FAMILY_APP')
            winapi_unicode = True

        emit('WIN32_WINNT', '{value}'.format(value=win32_winnt))
        defines.append('{name}=$WIN32_WINNT'.format(name=self.WIN32_WINNT.Macro))

        if winapi_unicode:
            defines += ['UNICODE', '_UNICODE']
        else:
            defines += ['_MBCS']

        # https://msdn.microsoft.com/en-us/library/abx4dbyh.aspx
        if is_positive('DLL_RUNTIME'):  # XXX
            flags_debug += ['/MDd']
            flags_release += ['/MD']
        else:
            flags_debug += ['/MTd']
            flags_release += ['/MT']

        if not self.tc.ide_msvs:
            for name in ('shared', 'ucrt', 'um', 'winrt'):
                flags.append('/I"{kit_includes}\\{name}"'.format(kit_includes=self.tc.kit_includes, name=name))
            flags.append('/I"{vc_root}\\include"'.format(vc_root=self.tc.vc_root))

        if self.tc.ide_msvs:
            flags += ['/FD', '/MP']
            debug_info_flags = '/Zi /FS'
        else:
            debug_info_flags = '/Z7'

        defines = self._gen_defines(defines)
        flags_werror = ['/WX']
        flags_sfdl = ['/E', '/C', '/P', '/Fi$SFDL_TMP_OUT']
        flags_no_optimize = ['/Od']
        flags_no_shadow = ['/wd4456', '/wd4457']
        flags_no_compiler_warnings = ['/w']

        emit('CXX_COMPILER', self.tc.cxx_compiler)
        emit('C_COMPILER', self.tc.c_compiler)
        emit('MASM_COMPILER', self.tc.masm_compiler)
        append('C_DEFINES', defines)
        emit('CFLAGS_DEBUG', flags_debug)
        emit('CFLAGS_RELEASE', flags_release)
        emit('MASMFLAGS', '')
        emit('DEBUG_INFO_FLAGS', debug_info_flags)

        if self.build.is_release:
            emit('CFLAGS_PER_TYPE', '$CFLAGS_RELEASE')
        if self.build.is_debug:
            emit('CFLAGS_PER_TYPE', '$CFLAGS_DEBUG')
        if self.build.is_ide:
            emit('CFLAGS_PER_TYPE', '@[debug|$CFLAGS_DEBUG]@[release|$CFLAGS_RELEASE]')

        append('CFLAGS', flags, '$CFLAGS_PER_TYPE', '$DEBUG_INFO_FLAGS', '$C_DEFINES', '$USER_CFLAGS', '$USER_CFLAGS_GLOBAL')
        append('CXXFLAGS', '$CFLAGS', flags_cxx, '$USER_CXXFLAGS')
        append('CONLYFLAGS', flags_c_only, '$USER_CONLYFLAGS')

        print '''\
when ($NO_OPTIMIZE == "yes") {{
    OPTIMIZE = {no_opt}
}}
when ($NO_COMPILER_WARNINGS == "yes") {{
    CFLAGS += {no_warn}
}}
when ($NO_WSHADOW == "yes") {{
    CFLAGS += {no_shadow}
}}
'''.format(no_opt=' '.join(flags_no_optimize), no_warn=' '.join(flags_no_compiler_warnings), no_shadow=' '.join(flags_no_shadow))

        emit('SFDL_FLAG', flags_sfdl)
        emit('WERROR_FLAG', flags_werror)
        emit('WERROR_MODE', self.tc.werror_mode)

        if not self.tc.under_wine:
            emit('CL_WRAPPER', '${YMAKE_PYTHON}', '${input:"build/scripts/fix_msvc_output.py"}', 'cl')
            emit('ML_WRAPPER', '${YMAKE_PYTHON}', '${input:"build/scripts/fix_msvc_output.py"}', 'ml')
        else:
            emit('CL_WRAPPER')
            emit('ML_WRAPPER')

        print '''\
macro MSVC_FLAGS(Flags...) {
    CFLAGS($Flags)
}

macro _SRC_cpp(SRC, SRCFLAGS...) {
    .CMD=${cwd:ARCADIA_BUILD_ROOT} ${TOOLCHAIN_ENV} ${CL_WRAPPER} ${CXX_COMPILER} /c /Fo${output:SRC.obj} ${input;msvs_source:SRC} ${pre=/I :INCLUDE} ${CXXFLAGS} ${SRCFLAGS} ${hide;kv:"soe"} ${hide;kv:"p CC"} ${hide;kv:"pc yellow"}
}

macro _SRC_c(SRC, SRCFLAGS...) {
    .CMD=${cwd:ARCADIA_BUILD_ROOT} ${TOOLCHAIN_ENV} ${CL_WRAPPER} ${C_COMPILER} /c /Fo${output:SRC.obj} ${input;msvs_source:SRC} ${pre=/I :INCLUDE} ${CFLAGS} ${CONLYFLAGS} ${SRCFLAGS} ${hide;kv:"soe"} ${hide;kv:"p CC"} ${hide;kv:"pc yellow"}
}

macro _SRC_m(SRC, SRCFLAGS...) {
}

macro _SRC_masm(SRC, SRCFLAGS...) {
    .CMD=${cwd:ARCADIA_BUILD_ROOT} ${TOOLCHAIN_ENV} ${ML_WRAPPER} ${MASM_COMPILER} ${MASMFLAGS} ${SRCFLAGS} ''' + masm_io + ''' ${kv;hide:"p AS"} ${kv;hide:"pc yellow"}
}
'''

    @staticmethod
    def _gen_defines(defines):
        return ['/D{}'.format(s) for s in defines]


class MSVCLinker(MSVC, Linker):
    def __init__(self, tc, build):
        MSVC.__init__(self, tc, build)
        Linker.__init__(self, tc, build)

    def print_linker(self):
        super(MSVCLinker, self).print_linker()

        target = self.build.target

        linker = self.tc.link
        linker_lib = self.tc.lib

        def get_arch():
            if target.is_intel:
                if target.is_32_bit:
                    return 'X86', None, 'x86'
                elif target.is_64_bit:
                    return 'X64', 'amd64', 'x64'
            elif target.is_arm and target.is_32_bit:
                return 'ARM', 'arm', 'arm'

            raise Exception('Unknown target platform {}'.format(str(target)))

        machine, vc_lib_arch, kit_lib_arch = get_arch()

        libpaths = []
        if self.tc.kit_libs:
            libpaths.extend([os.path.join(self.tc.kit_libs, name, kit_lib_arch) for name in ('um', 'ucrt')])
        libpaths.append(os.path.join(*filter(None, [self.tc.vc_root, 'lib', vc_lib_arch])))
        if is_positive('USE_UWP'):
            libpaths.append(os.path.join(self.tc.vc_root, 'lib', 'store', 'references'))

        ignored_errors = [
            4221
        ]

        flag_machine = '/MACHINE:{}'.format(machine)

        flags_ignore = ['/IGNORE:{}'.format(code) for code in ignored_errors]

        flags_common = ['/NOLOGO', '/ERRORREPORT:PROMPT', '/SUBSYSTEM:CONSOLE', '/TLBID:1', '$MSVC_DYNAMICBASE', '/NXCOMPAT']
        flags_common += flags_ignore
        flags_common += [flag_machine]

        flags_debug_only = []
        flags_release_only = []

        if self.tc.ide_msvs:
            flags_common += ['/INCREMENTAL']
        else:
            flags_common += ['/INCREMENTAL:NO']

        # TODO(nslus): DEVTOOLS-1868 remove restriction.
        if not self.tc.under_wine:
            if self.tc.ide_msvs:
                flags_debug_only.append('/DEBUG:FASTLINK')
                flags_release_only.append('/DEBUG')
            else:
                # No FASTLINK for ya make, because resulting PDB would require .obj files (build_root's) to persist
                flags_common.append('/DEBUG')

        if not self.tc.ide_msvs:
            flags_common += ['/LIBPATH:"{}"'.format(path) for path in libpaths]

        link_flags_debug = flags_common + flags_debug_only
        link_flags_release = flags_common + flags_release_only
        link_flags_lib = flags_ignore + [flag_machine]

        stdlibs = [
            'kernel32.lib',
            'user32.lib',
            'gdi32.lib',
            'winspool.lib',
            'shell32.lib',
            'ole32.lib',
            'oleaut32.lib',
            'uuid.lib',
            'comdlg32.lib',
            'advapi32.lib',
            'crypt32.lib',
        ]

        emit('LINK_LIB_CMD', linker_lib)
        emit('LINK_EXE_CMD', linker)
        emit('LINK_LIB_FLAGS', link_flags_lib)
        emit('LINK_EXE_FLAGS_RELEASE', link_flags_release)
        emit('LINK_EXE_FLAGS_DEBUG', link_flags_debug)
        emit('LINK_STDLIBS', stdlibs)
        emit('LDFLAGS_GLOBAL', '')
        emit('LDFLAGS', '')
        emit('OBJADDE', '')

        if self.build.is_release:
            emit('LINK_EXE_FLAGS_PER_TYPE', '$LINK_EXE_FLAGS_RELEASE')
        if self.build.is_debug:
            emit('LINK_EXE_FLAGS_PER_TYPE', '$LINK_EXE_FLAGS_DEBUG')
        if self.build.is_ide and self.tc.ide_msvs:
            emit('LINK_EXE_FLAGS_PER_TYPE', '@[debug|$LINK_EXE_FLAGS_DEBUG]@[release|$LINK_EXE_FLAGS_RELEASE]')

        emit('LINK_EXE_FLAGS', '$LINK_EXE_FLAGS_PER_TYPE')

        # TODO(nslus): DEVTOOLS-1868 remove restriction.
        if self.tc.under_wine:
            emit('LINK_EXTRA_OUTPUT')
        else:
            emit('LINK_EXTRA_OUTPUT', '/PDB:${output;noext;rootrel:REALPRJNAME.pdb}')

        if not self.tc.under_wine:
            emit('LIB_WRAPPER', '${YMAKE_PYTHON}', '${input:"build/scripts/fix_msvc_output.py"}', 'lib')
            emit('LINK_WRAPPER', '${YMAKE_PYTHON}', '${input:"build/scripts/fix_msvc_output.py"}', 'link')
        else:
            emit('LIB_WRAPPER')
            emit('LINK_WRAPPER')

        emit('LINK_WRAPPER_DYNLIB', '${YMAKE_PYTHON}', '${input:"build/scripts/link_dyn_lib.py"}', '--arch', 'WINDOWS', '--target', '$TARGET')
        emit('EXPORTS_VALUE')

        emit("GENERATE_MF", '$YMAKE_PYTHON ${input:"build/scripts/generate_mf.py"}',
             '--build-root $ARCADIA_BUILD_ROOT --module-name $REALPRJNAME -o ${output;rootrel;pre=$MODULE_PREFIX;suf=$MODULE_SUFFIX.mf:REALPRJNAME}',
             '-t $MODULE_TYPE $NO_GPL_FLAG -Ya,lics $LICENSE_NAMES -Ya,peers ${rootrel:PEERS}',
             '${kv;hide:"p MF"} ${kv;hide:"pc light-green"}'
             )

        print '''\
when ($EXPORTS_FILE) {
    EXPORTS_VALUE=/DEF:${input:EXPORTS_FILE}
}

LINK_LIB=${GENERATE_MF} && ${TOOLCHAIN_ENV} ${cwd:ARCADIA_BUILD_ROOT} ${LIB_WRAPPER} ${LINK_LIB_CMD} /OUT:${qe;rootrel:TARGET} \
${qe;rootrel:AUTO_INPUT} $LINK_LIB_FLAGS ${hide;kv:"soe"} ${hide;kv:"p AR"} ${hide;kv:"pc light-red"}

LINK_EXE=${GENERATE_MF} && ${TOOLCHAIN_ENV} ${cwd:ARCADIA_BUILD_ROOT} ${LINK_WRAPPER} ${LINK_EXE_CMD} /OUT:${qe;rootrel:TARGET} \
${LINK_EXTRA_OUTPUT} ${qe;rootrel:SRCS_GLOBAL} ${qe;rootrel:AUTO_INPUT} $LINK_EXE_FLAGS $LINK_STDLIBS $LDFLAGS $LDFLAGS_GLOBAL $OBJADDE \
${qe;rootrel:PEERS} ${hide;kv:"soe"} ${hide;kv:"p LD"} ${hide;kv:"pc blue"}

LINK_DYN_LIB=${GENERATE_MF} && ${TOOLCHAIN_ENV} ${cwd:ARCADIA_BUILD_ROOT} ${LINK_WRAPPER} ${LINK_WRAPPER_DYNLIB} ${LINK_EXE_CMD} \
/DLL /OUT:${qe;rootrel:TARGET} ${LINK_EXTRA_OUTPUT} ${EXPORTS_VALUE} \
${qe;rootrel:SRCS_GLOBAL} ${qe;rootrel:AUTO_INPUT} ${qe;rootrel:PEERS} \
$LINK_EXE_FLAGS $LINK_STDLIBS $LDFLAGS $LDFLAGS_GLOBAL $OBJADDE ${hide;kv:"soe"} ${hide;kv:"p LD"} ${hide;kv:"pc blue"}

LINK_FAT_OBJECT=${GENERATE_MF} && $YMAKE_PYTHON ${input:"build/scripts/touch.py"} $TARGET ${kv;hide:"p LD"} ${kv;hide:"pc light-blue"} ${kv;hide:"show_out"}
'''


# TODO(somov): Rename!
Compilers = {
    'gnu': (GnuToolchain, GCC, LD),
    'clang': (GnuToolchain, Clang, LD),
    'msvc': (MSVCToolchain, MSVCCompiler, MSVCLinker),
}


class Ragel(object):
    def __init__(self):
        self.rlgen_flags = []
        self.ragel_flags = []
        self.ragel6_flags = []

    def configure_toolchain(self, build, compiler):
        if isinstance(compiler, MSVCCompiler):
            self.set_default_flags(optimized=False)
        elif isinstance(compiler, GnuCompiler):
            self.set_default_flags(optimized=build.is_release and not build.is_sanitized)
        else:
            raise ConfigureError('Unexpected compiler {}'.format(compiler))

    def set_default_flags(self, optimized):
        if optimized:
            self.rlgen_flags.append('-G2')
            self.ragel6_flags.append('-CG2')
        else:
            self.rlgen_flags.append('-T0')
            self.ragel6_flags.append('-CT0')

    def print_variables(self):
        emit('RLGEN_FLAGS', self.rlgen_flags)
        emit('RAGEL_FLAGS', self.ragel_flags)
        emit('RAGEL6_FLAGS', self.ragel6_flags)


class Python(object):
    def __init__(self, tc):
        self.python = None
        self.flags = None
        self.ldflags = None
        self.libraries = None
        self.includes = None
        self.tc = tc

    def configure_posix(self, python=None, python_config=None):
        python = python or preset('PYTHON_BIN') or which('python')
        python_config = python_config or preset('PYTHON_CONFIG') or which('python-config')

        if python is None or python_config is None:
            return

        # python-config dumps each option on one line in the specified order
        config = get_stdout([python_config, '--cflags', '--ldflags', '--includes']) or ''
        config = config.split('\n')
        if len(config) < 3:
            return

        self.python = python
        self.flags = config[0]
        self.ldflags = config[1]
        self.includes = config[2]
        # Do not split libraries from ldflags.
        # They are not used separately and get overriden together, so it is safe.
        # TODO(somov): Удалить эту переменную и PYTHON_LIBRARIES из makelist-ов.
        self.libraries = ''
        if preset('USE_ARCADIA_PYTHON') == 'no' and not preset('USE_SYSTEM_PYTHON') and not self.tc.os_sdk_local:
            raise Exception('System non fixed python can be used only with OS_SDK=local')

    def print_variables(self):
        variables = Variables({
            'PYTHON_BIN': self.python,
            'PYTHON_FLAGS': self.flags,
            'PYTHON_LDFLAGS': self.ldflags,
            'PYTHON_LIBRARIES': self.libraries,
            'PYTHON_INCLUDE': self.includes
        })

        variables.update_from_presets()
        variables.reset_if_any(reset_value='PYTHON-NOT-FOUND')
        variables.emit()


class Perl(object):
    # Parse (key, value) from "version='5.26.0';" lines
    PERL_CONFIG_RE = re.compile(r"^(?P<key>\w+)='(?P<value>.*)';$", re.MULTILINE)

    def __init__(self):
        self.perl = None
        self.version = None
        self.privlib = None
        self.archlib = None

    def configure_local(self, perl=None):
        self.perl = perl or preset('PERL') or which('perl')
        if self.perl is None:
            return

        config = dict(self._iter_config(['version', 'privlibexp', 'archlibexp']))
        self.version = config.get('version')
        self.privlib = config.get('privlibexp')
        self.archlib = config.get('archlibexp')

    def print_variables(self, prefix=''):
        variables = Variables({
            prefix + 'PERL': self.perl,
            prefix + 'PERL_VERSION': self.version,
            prefix + 'PERL_PRIVLIB': self.privlib,
            prefix + 'PERL_ARCHLIB': self.archlib,
        })

        variables.reset_if_any(reset_value='PERL-NOT-FOUND')
        variables.emit()

    def _iter_config(self, config_keys):
        # Run perl -V:version -V:etc...
        perl_config = [self.perl] + ['-V:{}'.format(key) for key in config_keys]
        config = get_stdout(perl_config) or ''

        start = 0
        while True:
            match = Perl.PERL_CONFIG_RE.search(config, start)
            if match is None:
                break
            yield match.group('key', 'value')
            start = match.end()


class Cuda(object):
    def __init__(self, build):
        """
        :type build: Build
        """
        self.build = build

    def print_variables(self):
        have_cuda = is_positive('HAVE_CUDA') or self._have_cuda()

        if preset('HAVE_CUDA') is None:
            emit('HAVE_CUDA', have_cuda)

        use_arcadia_cuda = preset('CUDA_ROOT') is None
        emit('_USE_ARCADIA_CUDA', use_arcadia_cuda)

        nvcc_flags = []

        if use_arcadia_cuda:
            emit('CUDA_ROOT', '$(CUDA)')

            cuda_compiler = self.get_cuda_compiler()
            if cuda_compiler is not None:
                nvcc_flags.append('--compiler-bindir={}'.format(cuda_compiler))

            target = self.build.target
            if target.is_linux:
                if target.is_x86_64:
                    if self.build.tc.is_clang:
                        os_sdk_root = '{OS_SDK_ROOT}' if self.build.tc.version_at_least(4, 0) else ''
                        nvcc_flags.append('-I${}/usr/include/x86_64-linux-gnu'.format(os_sdk_root))

        emit('NVCC_UNQUOTED', '$CUDA_ROOT\\bin\\nvcc.exe' if self.build.host.is_windows else '$CUDA_ROOT/bin/nvcc')
        emit('NVCC', '${quo:NVCC_UNQUOTED}')

        if preset('CUDA_NVCC_FLAGS') is None:
            emit('CUDA_NVCC_FLAGS')

        nvcc_flags.append('$CUDA_NVCC_FLAGS')
        emit('NVCC_FLAGS', nvcc_flags)

    def get_cuda_compiler(self):
        target = self.build.target

        user_compiler = preset('CUDA_COMPILER')
        if user_compiler is not None:
            return user_compiler

        if target.is_linux:
            if target.is_x86_64:
                return '$(CUDA)/compiler/gcc/bin/g++-4.9'
            elif target.is_aarch64:
                return '$(CUDA)/compiler/gcc/bin/aarch64-linux-g++'

        elif target.is_macos:
            if target.is_x86_64:
                return '$(CUDA_XCODE)/usr/bin'

        return None

    def _have_cuda(self):
        if preset('CUDA_ROOT') is not None:
            return True
        if is_negative('USE_ARCADIA_CUDA'):
            return False

        host = self.build.host
        target = self.build.target

        if host.is_linux and host.is_x86_64:
            if target.is_linux:
                return target.is_x86_64 or target.is_aarch64

        if host.is_macos and host.is_x86_64:
            if target.is_macos:
                return target.is_x86_64

        return False


class Yasm(object):
    def __init__(self, target):
        self.yasm_tool = '${tool:"contrib/tools/yasm"}'
        self.fmt = None
        self.platform = None
        self.target = target
        self.flags = []

    def configure(self):
        if self.target.is_ios or self.target.is_macos:
            self.platform = ['DARWIN', 'UNIX']
            self.fmt = 'macho'
        elif (self.target.is_windows and self.target.is_64_bit) or self.target.is_cygwin:
            self.platform = ['WIN64']
            self.fmt = 'win'
        elif self.target.is_windows and self.target.is_32_bit:
            self.platform = ['WIN32']
            self.fmt = 'win'
        else:
            self.platform = ['UNIX']
            self.fmt = 'elf'

        if self.fmt == 'elf':
            self.flags += ['-g', 'dwarf2']

    def print_variables(self):
        d_platform = ' '.join([('-D ' + i) for i in self.platform])
        output = '${{output;noext:SRC.{}}}'.format('o' if self.fmt != 'win' else 'obj')
        print '''\
macro _SRC_yasm_impl(SRC, PREINCLUDES[], SRCFLAGS...) {{
    .CMD={} -f {}$HARDWARE_ARCH {} -D ${{pre=_;suf=_:HARDWARE_TYPE}} -D_YASM_ $ASM_PREFIX_VALUE {} ${{YASM_FLAGS}} ${{pre=-I :INCLUDE}} -o {} ${{pre=-P :PREINCLUDES}} ${{input:SRC}} ${{kv;hide:"p AS"}} ${{kv;hide:"pc light-green"}} ${{input;hide:PREINCLUDES}}

}}
'''.format(self.yasm_tool, self.fmt, d_platform, ' '.join(self.flags), output)


def main():
    options = opts()

    arcadia = Arcadia(options.arcadia_root)

    ymake = YMake(arcadia)

    ymake.print_core_conf()
    ymake.print_presets()
    ymake.print_settings()

    build = Build(arcadia, options.build_type, options.toolchain_params, force_ignore_local_files=not options.local_distbuild)
    build.print_build()

    emit('CONF_SCRIPT_DEPENDS', __file__)


if __name__ == '__main__':
    main()
