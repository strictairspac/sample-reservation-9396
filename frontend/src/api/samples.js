import {api} from './client.js';
export const samplesApi={list:()=>api('/samples'),create:data=>api('/samples','POST',data),update:(id,data)=>api('/samples/'+id,'PUT',data)};
