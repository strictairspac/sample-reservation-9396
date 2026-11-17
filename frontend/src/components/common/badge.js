import {RUN_STATES,RESERVATION_RUN_STATES,RESERVATION_STATES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';
export const badge=value=>`<span class="badge ${esc(value)}">${esc(RUN_STATES[value]||RESERVATION_RUN_STATES[value]||RESERVATION_STATES[value]||value)}</span>`;
