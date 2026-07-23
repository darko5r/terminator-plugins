project(
  'terminator-full-select-native',
  'c',
  version: '0.2.0',
  meson_version: '>=1.4.0',
  default_options: [
    'c_std=c17',
    'warning_level=3',
    'werror=true',
    'b_lto=false',
  ],
)

cc = meson.get_compiler('c')
math_dependency = cc.find_library('m', required: true)

precision_arguments = [
  '-fno-fast-math',
  '-ffp-contract=off',
  '-fvisibility=hidden',
]

foreach argument : precision_arguments
  if not cc.has_argument(argument)
    error('Required precision argument is unsupported: ' + argument)
  endif
endforeach

shared_library(
  'terminator_full_select_engine',
  'tfse_engine.c',
  dependencies: math_dependency,
  c_args: precision_arguments,
  gnu_symbol_visibility: 'hidden',
  install: false,
  name_prefix: 'lib',
)
