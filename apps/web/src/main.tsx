/* Entry point.
   Routes:
     /          → main workspace (Base Station UI — Dockview workspace, slice-4)
     /popout/:vizType?receiverId=...&config=... → single-visualization popout window
*/
import { render } from 'solid-js/web';
import { Router, Route } from '@solidjs/router';
import 'dockview-core/dist/styles/dockview.css';
import './styles/globals.css';

import { MainRoute } from './routes/main';
import { PopoutRoute } from './routes/popout';

const root = document.getElementById('root');
if (!root) throw new Error('#root not found');

render(
  () => (
    <Router>
      <Route path="/" component={MainRoute} />
      <Route path="/popout/:vizType" component={PopoutRoute} />
    </Router>
  ),
  root,
);
