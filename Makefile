.PHONY: run test generate check visual-update

run:
	./bin/white-pill-studio

test:
	python -m unittest discover -s tests

generate:
	./script/design-system generate

check:
	./script/design-system check
	python -m unittest discover -s tests
	./script/visual-baseline check --skip-if-unavailable

visual-update:
	./script/visual-baseline update

