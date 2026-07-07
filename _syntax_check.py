import ast, os, sys

errors = []
for root, dirs, files in os.walk(sys.argv[1] if len(sys.argv) > 1 else 'src'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        try:
            with open(path) as fh: ast.parse(fh.read())
        except SyntaxError as e:
            errors.append('SYNTAX ERROR in %s: %s' % (path, e))
if errors:
    for e in errors: print(e)
    sys.exit(1)
else:
    print('All Python files parse without syntax errors')
