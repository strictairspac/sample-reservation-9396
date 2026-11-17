import {reservationsApi} from '../api/reservations.js';
export const reservationsStore={summary:null,items:[],async refresh(){const [summary,items]=await Promise.all([reservationsApi.summary(),reservationsApi.list()]);this.summary=summary;this.items=items;return summary;}};
