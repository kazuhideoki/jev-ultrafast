"""Real local-browser dropdown races and uncertain results; no model calls."""

import time
from contextlib import closing, contextmanager
from unittest.mock import patch
from urllib.parse import quote

from jev_ultrafast import agent as loop
from jev_ultrafast import browser as execution

HTML = """<!doctype html><title>Dropdown checks</title>
<style>body{margin:30px}select{width:200px;height:40px}</style>
<label>Category<select id="category"><option>All</option>
<optgroup label="Choices"><option value="design">Design</option></optgroup></select></label>
<script>
window.select=document.querySelector('select'); window.events=[];
for (const name of ['input','change']) select.addEventListener(name,()=>events.push(name));
</script>"""


def decide(page, _goal, _history):
    action = next((a for a in page['actions'] if a['kind'] == 'select'), None)
    selected = action['id'] if action else 'DONE'
    return dict(choice=selected, operation='SELECT' if action else 'DONE', target='1',
                confidence=1, probabilities={selected: 1}, latency_ms=0, usage={})


@contextmanager
def local_agent(browser):
    # Reuse one owned tab/session; each case still gets a new document and Agent state.
    browser.call('Page.navigate', url='data:text/html,' + quote(HTML))
    for _ in range(100):
        if browser.evaluate("document.readyState==='complete' && typeof window.events!=='undefined'"):
            break
        time.sleep(0.02)
    browser.after_input = None
    with patch.object(loop, 'Browser', return_value=browser), patch.object(browser, 'close'):
        with loop.Agent('about:blank', 'Select Design') as agent:
            yield agent


def main():
    with closing(execution.Browser('about:blank')) as browser:
        check(browser)


def check(browser):
    passed = []
    with patch.object(loop, 'choose', side_effect=decide):
        for reason, mutation in {
            'target_missing': 'select.remove()',
            'target_disabled': 'select.disabled=true',
            'target_hidden': "select.style.display='none'",
            'target_outside_viewport': "select.style.position='absolute';select.style.top='3000px'",
            'target_covered': "window.cover=document.createElement('div');"
                              "cover.style.cssText='position:fixed;inset:0;z-index:9999';document.body.append(cover)",
            'option_missing': 'select.options[1].remove()',
            'option_changed': "select.options[1].label='Other'",
            'option_disabled': 'select.options[1].disabled=true',
            'optgroup_disabled': "select.querySelector('optgroup').disabled=true",
            'option_replaced': 'select.options[1].outerHTML=select.options[1].outerHTML',
        }.items():
            with local_agent(browser) as agent:
                original_fresh = agent.browser.fresh

                def race(page, action=None):
                    fresh = original_fresh(page, action)
                    # Inject AFTER the freshness read, immediately before executor validation.
                    if action:
                        agent.browser.evaluate(mutation)
                    return fresh

                with patch.object(agent.browser, 'fresh', side_effect=race):
                    state = agent.command('tick')
                expected = {
                    'optgroup_disabled': 'option_disabled', 'option_replaced': 'option_missing',
                }.get(reason, reason)
                assert state['status'] == 'ready' and state['decision'] is None, reason
                assert state['failures'][-1]['reason'] == expected, state['failures']
                assert state['failures'][-1]['mutation_started'] is False
                assert agent.browser.evaluate('[select.value,events]') == ['All', []], reason
                assert not state['history']
                if reason == 'target_covered':
                    agent.browser.evaluate('cover.remove()')
                    state = agent.command('tick')
                    assert state['status'] == 'ready' and len(state['history']) == 1
                    assert agent.browser.evaluate('[select.value,events]') == ['design', ['input', 'change']]
                    assert len(state['decisions']) == 2
                    passed.append('reobserved, redecided and executed exactly once after cover removed')
                passed.append(reason + ': rejected before value change or events')

        with local_agent(browser) as agent:
            agent.browser.evaluate("window.cover=document.createElement('div');"
                                   "cover.style.cssText='position:fixed;inset:0;z-index:9999';document.body.append(cover)")
            page = agent.browser.observe(screenshot=False)
            assert not any(a['kind'] == 'select' for a in page['actions'])
            assert agent.browser.evaluate('[select.value,events]') == ['All', []]
            passed.append('persistent cover: unavailable selection omitted before model judgment')

        for mode in ('exception_after_input', 'lost_response'):
            with local_agent(browser) as agent:
                if mode == 'exception_after_input':
                    agent.browser.evaluate("select.dispatchEvent=function(event){"
                                           "EventTarget.prototype.dispatchEvent.call(this,event);"
                                           "if(event.type==='input')throw Error('test');}")
                real_cdp = execution.cdp
                mutations = []

                def intercept(method, **params):
                    result = real_cdp(method, **params)
                    if method == 'Runtime.evaluate' and 'let started=false' in params.get('expression', ''):
                        mutations.append(1)
                        if mode == 'lost_response':
                            return {'result': {}}
                    return result

                with patch.object(execution, 'cdp', side_effect=intercept):
                    states = list(agent.run())
                    assert list(agent.run()) == []
                assert len(mutations) == 1
                assert len(states) == 1 and states[0]['status'] == 'blocked'
                started = True if mode == 'exception_after_input' else None
                assert states[0]['failures'][-1]['mutation_started'] is started
                expected_events = ['input'] if mode == 'exception_after_input' else ['input', 'change']
                assert agent.browser.evaluate('[select.value,events]') == ['design', expected_events]
                passed.append(mode + ': blocked, no second selection')
    print('\n'.join(passed))
    print(f'PASS: {len(passed)} dropdown checks; no model calls')


if __name__ == '__main__':
    main()
