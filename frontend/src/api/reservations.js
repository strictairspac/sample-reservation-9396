import {api} from './client.js';
export const reservationsApi={
  list:()=>api('/reservations'),
  summary:()=>api('/reservations/summary'),
  enable:data=>api('/reservations/enable','POST',data),
  retry:id=>api('/reservations/runs/'+id+'/retry','POST',{}),
  confirm:id=>api('/reservations/'+id+'/confirm','POST',{}),
  cancel:id=>api('/reservations/'+id+'/cancel','POST',{})
};
